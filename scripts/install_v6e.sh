set -euo pipefail

cd
gsutil -m cp -r gs://kmh-gcp-us-east1/hanhong/v6_wheels.tar.gz ./wheels.tar.gz
tar -xvf wheels.tar.gz
rm -rf .local || true
pip install --no-index --find-links=wheels wheels/*.whl --no-deps --force-reinstall
rm -rf wheels wheels.tar.gz