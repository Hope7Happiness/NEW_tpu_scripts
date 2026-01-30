# This script is not designed to be run. It is runned once to create gs://kmh-gcp-us-east1/hanhong/v6_wheels.tar.gz
set -ex

rm -rf .local
mkdir -p wheels
cd wheels

pip install setuptools==69.5.1 # important, otherwise some may fail to build
pip download jax[tpu]==0.4.37 jaxlib -f https://storage.googleapis.com/jax-releases/libtpu_releases.html
pip download flax>=0.8
pip download pillow clu tensorflow==2.15.0 \"keras<3\" \"torch<=2.4\" torchvision tensorflow_datasets matplotlib==3.9.2
pip download diffusers dm-tree cached_property ml-collections transformers==4.38.2 lpips_j
pip download wandb gcsfs
pip download orbax-checkpoint==0.6.4

rm -rf flax-0.10.7-py3-none-any.whl jax-0.4.37-py3-none-any.whl jax-0.6.2-py3-none-any.whl jaxlib-0.4.36-cp310-cp310-manylinux2014_x86_64.whl jaxlib-0.6.2-cp310-cp310-manylinux2014_x86_64.whl ml_dtypes-0.2.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl numpy-2.2.6-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl optax-0.2.6-py3-none-any.whl orbax_checkpoint-0.11.32-py3-none-any.whl  orbax_checkpoint-0.4.4-py3-none-any.whl protobuf-4.25.8-cp37-abi3-manylinux2014_x86_64.whl tensorstore-0.1.45-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl wrapt-2.0.1-cp310-cp310-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl

ls | grep -v pyasn | sed -E 's/[0-9].*//' | awk '
NR == 1 { prev = $0; next }
$0 == prev { print "Error: duplicate prefix \"" $0 "\" on line " NR > "/dev/stderr";}
{ prev = $0 }
END { print "OK: no duplicates." > "/dev/stderr" }
'

cd ..
tar -czvf v5_wheels.tar.gz wheels
gsutil -m cp -r ./v5_wheels.tar.gz gs://kmh-gcp-us-east5/hanhong/v5_wheels.tar.gz