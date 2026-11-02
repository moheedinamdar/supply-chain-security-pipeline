#!/usr/bin/env bash
# Admission and GitOps proof against a kind cluster. CI only: it pushes test images and signs with OIDC.
set -Eeuo pipefail
# Run the last pipeline stage in this shell so expect_* can update the failure counter.
shopt -s lastpipe

: "${IMAGE:?}" "${DIGEST:?}" "${MANIFESTS:?}" "${MANIFESTS_DIGEST:?}" "${KYVERNO_CHART_VERSION:?}"

SIGNED="${IMAGE}@${DIGEST}"
THIRD_PARTY="docker.io/library/nginx:1.30@sha256:51047bea59fed88fbd1f52565d2f5fabcb82d311cf6c9e507a10ce74bb3bc3f0"
RUN_ID="${GITHUB_RUN_ID:-local}"
workdir=$(mktemp -d)
failures=0

# Annotations are readable through the public checks API; raw job logs need a GitHub login.
annotate() { # level message
  [[ -n "${GITHUB_ACTIONS:-}" ]] || return 0
  local msg=${2//'%'/'%25'}
  msg=${msg//$'\r'/'%0D'}
  msg=${msg//$'\n'/'%0A'}
  echo "::$1 title=e2e::${msg}"
}

pass() { echo "PASS: $1"; annotate notice "PASS: $1"; }
fail() { # description [detail]
  echo "FAIL: $1"
  [[ -n "${2:-}" ]] && echo "$2"
  annotate error "FAIL: $1${2:+
$2}"
  failures=$((failures + 1))
}

flux_status() { # kind/name...
  kubectl -n flux-system get "$@" -o json | jq -r '(.items // [.])[]
    | "\(.kind)/\(.metadata.name): " + ([.status.conditions[]? | "\(.type)=\(.status) \(.message)"] | join("; "))'
}

# Setup failures abort under set -e before any assertion runs; surface them as annotations too.
touch "$workdir/stderr.log"
exec 2> >(tee -a "$workdir/stderr.log" >&2)
trap 'annotate error "e2e aborted at line ${LINENO}: ${BASH_COMMAND}
$(tail -c 900 "$workdir/stderr.log")"' ERR

container() { # name image privileged
  local escalate=false
  [[ "$3" == true ]] && escalate=true
  cat <<EOF
    - name: $1
      image: $2
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        privileged: $3
        allowPrivilegeEscalation: ${escalate}
        readOnlyRootFilesystem: true
      resources:
        limits:
          cpu: 100m
          memory: 64Mi
EOF
}

pod() { # name namespace image [privileged | nolabels | init=<image>]
  local name=$1 ns=$2 image=$3 variant=${4:-}
  local privileged=false init="" labels
  labels=$'    team: platform-security\n    cost-center: portfolio'
  case "$variant" in
    privileged) privileged=true ;;
    nolabels) labels='    e2e: "true"' ;;
    init=*) init=${variant#init=} ;;
  esac
  cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${ns}
  labels:
${labels}
spec:
EOF
  if [[ -n "$init" ]]; then
    echo "  initContainers:"
    container init "$init" false
  fi
  echo "  containers:"
  container app "$image" "$privileged"
}

expect_admit() { # description < manifest
  local err="$workdir/err"
  cat > "$workdir/manifest.yaml"
  if kubectl apply --dry-run=server -f "$workdir/manifest.yaml" 2> "$err"; then
    pass "$1"
  else
    fail "$1 (denied)" "$(head -c 700 "$err")"
  fi
}

expect_deny() { # description expected-policy < manifest
  local err="$workdir/err"
  cat > "$workdir/manifest.yaml"
  if kubectl apply --dry-run=server -f "$workdir/manifest.yaml" 2> "$err"; then
    fail "$1 (admitted)"
  elif grep -q -- "$2" "$err"; then
    pass "$1"
    sed 's/^/    /' "$err"
  else
    fail "$1 (denied, but not by $2)" "$(head -c 700 "$err")"
  fi
}

# Pushes a rebuilt copy of the signed image under a new digest that nothing has signed.
variant() { # suffix
  printf 'FROM %s\nLABEL e2e.variant=%s e2e.run=%s\n' "$SIGNED" "$1" "$RUN_ID" |
    docker buildx build --push --provenance=false --sbom=false \
      --metadata-file "$workdir/$1.json" -t "${IMAGE}:e2e-$1" - >&2
  echo "${IMAGE}@$(jq -r '."containerimage.digest"' "$workdir/$1.json")"
}

echo "== Installing Kyverno (chart ${KYVERNO_CHART_VERSION})"
helm install kyverno kyverno --repo https://kyverno.github.io/kyverno \
  --version "$KYVERNO_CHART_VERSION" --namespace kyverno --create-namespace \
  --wait --timeout 5m

kubectl apply -f deploy/cluster/namespaces.yaml
kubectl apply -f policies/kyverno/

echo "== Waiting for the policies to reach the admission webhook"
active=false
for _ in $(seq 1 36); do
  if ! pod probe prod "$THIRD_PARTY" | kubectl apply --dry-run=server -f - 2> "$workdir/err" &&
    grep -q verify-image-signatures "$workdir/err"; then
    active=true
    break
  fi
  sleep 5
done
if [[ "$active" != true ]]; then
  fail "policies never became active" "$(head -c 700 "$workdir/err")"
  kubectl get imagevalidatingpolicies,validatingpolicies -o yaml
  exit 1
fi

echo "== Preparing bad images"
UNSIGNED=$(variant unsigned)
WRONG_SIGNER=$(variant wrong-signer)
# Valid Sigstore signature, but the identity is release.yml, not the trusted build workflow.
cosign sign --yes "$WRONG_SIGNER"

echo "== Admission: image verification"
pod signed prod "$SIGNED" | expect_admit "signed, attested image is admitted in prod"
pod nginx prod "$THIRD_PARTY" | expect_deny "unsigned third-party image is denied" verify-image-signatures
pod unsigned prod "$UNSIGNED" | expect_deny "unsigned build of this repo is denied" verify-image-signatures
pod wrong-signer prod "$WRONG_SIGNER" | expect_deny "image signed by another workflow is denied" verify-image-signatures
pod sneaky-init prod "$SIGNED" "init=$THIRD_PARTY" | expect_deny "unsigned init container is denied" verify-image-signatures

echo "== Admission: pod hardening"
pod privileged prod "$SIGNED" privileged | expect_deny "privileged container is denied" pod-hardening
pod unlabelled prod "$SIGNED" nolabels | expect_deny "pod without team/cost-center labels is denied" pod-hardening

echo "== Scope"
pod outside sandbox "$THIRD_PARTY" | expect_admit "unsigned image is admitted outside enforced namespaces"

echo "== GitOps: Flux deploys only signed manifests"
flux install --components=source-controller,kustomize-controller
sed "s|__DIGEST__|${MANIFESTS_DIGEST}|" deploy/flux/source.yaml | kubectl apply -f -
kubectl apply -f deploy/flux/sync.yaml
if kubectl -n flux-system wait ocirepository/app \
  --for=jsonpath='{.status.conditions[?(@.type=="SourceVerified")].status}'=True --timeout=3m &&
  kubectl -n flux-system wait kustomization/app --for=condition=Ready --timeout=5m &&
  kubectl -n prod rollout status deployment/supply-chain-demo --timeout=2m; then
  pass "signed manifests are verified by Flux and the signed image runs in prod"
  kubectl -n prod get pods -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.containers[0].image}{"\n"}{end}'
else
  fail "signed manifests did not deploy" "$(flux_status ocirepository/app kustomization/app | head -c 700)"
  flux -n flux-system get all
  kubectl -n prod get events --sort-by=.lastTimestamp
fi

mkdir -p "$workdir/tampered"
sed -e "s|__IMAGE__|${SIGNED}|" -e 's/replicas: 1/replicas: 5/' deploy/app/deployment.yaml > "$workdir/tampered/deployment.yaml"
tampered=$(flux push artifact "oci://${MANIFESTS}:e2e-unsigned" \
  --path="$workdir/tampered" \
  --source="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-local}" \
  --revision="e2e@sha1:${GITHUB_SHA:-0000000000000000000000000000000000000000}" \
  --output json | jq -r '.digest')
sed -e "s|__DIGEST__|${tampered}|" -e 's/^  name: app$/  name: app-unsigned/' deploy/flux/source.yaml | kubectl apply -f -
if kubectl -n flux-system wait ocirepository/app-unsigned \
  --for=jsonpath='{.status.conditions[?(@.type=="SourceVerified")].status}'=False --timeout=3m; then
  pass "unsigned manifests are rejected by Flux"
  kubectl -n flux-system get ocirepository/app-unsigned \
    -o jsonpath='{.status.conditions[?(@.type=="SourceVerified")].message}{"\n"}'
else
  fail "unsigned manifests were not rejected" "$(flux_status ocirepository/app-unsigned | head -c 700)"
  kubectl -n flux-system get ocirepository/app-unsigned -o yaml
fi

echo
if ((failures > 0)); then
  echo "${failures} assertion(s) failed"
  kubectl -n kyverno logs deployment/kyverno-admission-controller --tail=200
  exit 1
fi
echo "All assertions passed"
