#!/usr/bin/env bash
# Fills SSM /tutoria/<env>/worker/ (what the worker reads on ECS, AWS_SSM_PREFIX)
# from what SSM already holds for the other two backends. Run it yourself,
# signed in with `aws login`:
#
#   deploy/aws/seed-ssm.sh dev            what would be written (names only)
#   deploy/aws/seed-ssm.sh dev --apply    write the missing ones (SecureString)
#
#   /tutoria/<env>/ai-api/<NAME>        -> worker/<NAME>, for every NAME the
#                                          worker's settings read (app/core/config.py)
#   /tutoria/<env>/Sqs/ExtractionQueueUrl -> worker/SQS_EXTRACTION_QUEUE_URL (and the
#                                          other four queues of the .NET API)
#
# Never overwrites, never prints a value. What only Railway had (e.g.
# ASSEMBLYAI_API_KEY, STUDENT_APP_EXPO_ACCESS_TOKEN) you add by hand under the
# same folder; the list of the worker's settings is in app/core/config.py.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077

ENV=${1:?usage: seed-ssm.sh dev|prod [--apply]}
APPLY=${2:-}
REGION=us-east-2
SRC="/tutoria/$ENV"
DST="/tutoria/$ENV/worker"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

grep -oE '^    [A-Z][A-Z0-9_]+:' app/core/config.py | tr -d ' :' > "$TMP/fields"
aws ssm get-parameters-by-path --region "$REGION" --path "$DST/" --query "Parameters[].Name" --output text \
  | tr '\t' '\n' | sed "s#^$DST/##" > "$TMP/existing"

put() { # name source-parameter
  local name=$1 source=$2
  if grep -qx "$name" "$TMP/existing"; then echo "  $DST/$name: already set, kept"; return; fi
  if [ "$APPLY" != --apply ]; then echo "  $DST/$name: would copy from $source"; return; fi
  aws ssm get-parameter --region "$REGION" --name "$source" --with-decryption --query Parameter.Value --output text > "$TMP/value"
  aws ssm put-parameter --region "$REGION" --name "$DST/$name" --type SecureString --value "file://$TMP/value" >/dev/null
  rm -f "$TMP/value"
  echo "  $DST/$name: copied from $source"
}

aws ssm get-parameters-by-path --region "$REGION" --path "$SRC/ai-api/" --query "Parameters[].Name" --output text \
  | tr '\t' '\n' | sed "s#^$SRC/ai-api/##" | while read -r name; do
      if [ -n "$name" ] && grep -qx "$name" "$TMP/fields"; then put "$name" "$SRC/ai-api/$name"; fi
    done

for pair in ExtractionQueueUrl:SQS_EXTRACTION_QUEUE_URL QuizGenQueueUrl:SQS_QUIZ_GEN_QUEUE_URL \
            TranscriptionQueueUrl:SQS_TRANSCRIPTION_QUEUE_URL GradingQueueUrl:SQS_GRADING_QUEUE_URL \
            QuizUploadQueueUrl:SQS_QUIZ_UPLOAD_QUEUE_URL; do
  if aws ssm get-parameter --region "$REGION" --name "$SRC/Sqs/${pair%%:*}" --query Parameter.Name --output text >/dev/null 2>&1; then
    put "${pair##*:}" "$SRC/Sqs/${pair%%:*}"
  fi
done

[ "$APPLY" = --apply ] || echo "Nothing written. Run with --apply to copy the missing ones."
