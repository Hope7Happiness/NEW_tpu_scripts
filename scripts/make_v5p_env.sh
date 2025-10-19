# This script is not designed to be run. It is runned once to create gs://kmh-gcp-us-east1/hanhong/v6_wheels.tar.gz
set -ex

rm -rf .local
mkdir -p wheels
cd wheels

pip install setuptools==69.5.1 # important, otherwise some may fail to build
pip download jax==0.4.27 'jax[tpu]==0.4.27' -f https://storage.googleapis.com/jax-releases/libtpu_releases.html # this is only for tpu libs
pip download jaxlib==0.4.27 "flax>=0.8"
pip download requests==2.32.3 opt_einsum==3.4.0 scipy==1.15.3 idna==3.3 charset-normalizer==3.4.0 certifi==2020.6.20 urllib3==1.26.5
pip download setuptools==69.5.1 opt-einsum==3.4.0 six==1.16.0 packaging==21.3 pyparsing==2.4.7 jinja2==3.0.3 filelock==3.16.1 wheel==0.37.1 zipp==1.0.0 charset-normalizer==3.4.0 attrs==21.2.0 PyYAML==5.4.1 pyasn1-modules==0.2.1 oauthlib==3.2.0 pyasn1==0.4.8 MarkupSafe==3.0.3 absl-py==2.3.1 array_record==0.8.1 astunparse==1.6.3 cachetools==6.2.0 chex==0.1.90 clu==0.0.12 contourpy==1.3.2 cycler==0.12.1 dm-tree==0.1.9 docstring-parser==0.17.0 einops==0.8.1 etils==1.13.0 flatbuffers==25.9.23 flax==0.10.4 fonttools==4.60.1 fsspec==2025.9.0 gast==0.6.0 google-auth==2.41.1 google-auth-oauthlib==1.2.2 google-pasta==0.2.0 grpcio==1.75.1 h5py==3.14.0 immutabledict==4.2.1 importlib_resources==6.5.2 keras==2.15.0 kiwisolver==1.4.9 libclang==18.1.1 markdown==3.9 markdown-it-py==4.0.0 matplotlib==3.9.2 mdurl==0.1.2 ml-collections==1.1.0 ml-dtypes==0.2.0 mpmath==1.3.0 msgpack==1.1.2 nest_asyncio==1.6.0 networkx==3.4.2 numpy==1.26.4 optax==0.2.5 orbax-checkpoint==0.4.4 pillow==11.3.0 protobuf==4.21.12 psutil==7.1.0 pyarrow==21.0.0 pygments==2.19.2 python-dateutil==2.9.0.post0 requests-oauthlib==2.0.0 rich==14.1.0 rsa==4.9.1 simple_parsing==0.1.7 sympy==1.14.0 tensorboard==2.15.2 tensorboard-data-server==0.7.2 tensorflow-cpu==2.15.0 tensorflow-estimator==2.15.0 tensorflow-io-gcs-filesystem==0.37.1 tensorflow-metadata==1.17.2 tensorflow_datasets==4.9.9 tensorstore==0.1.45 termcolor==3.1.0 toml==0.10.2 toolz==1.0.0 tqdm==4.67.1 treescope==0.1.10 typing-extensions==4.15.0 werkzeug==3.1.3 wrapt==1.14.2

pip download numpy==1.26.4 fsspec==2025.9.0 networkx==3.4.2 sympy==1.14.0 markupsafe==3.0.3 jinja2==3.0.3 pillow==11.3.0 typing-extensions==4.15.0 filelock==3.16.1 torch==2.4.0 torchvision==0.19.0  --extra-index-url https://download.pytorch.org/whl/cpu

rm tensorstore-0.1.45-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl ml_dtypes-0.2.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl orbax_checkpoint-0.4.4-py3-none-any.whl

pip download msgpack==1.1.2 pyyaml==5.4.1 protobuf==4.21.12 etils==1.13.0 numpy==1.26.4 humanize==4.13.0 typing-extensions==4.15.0 nest_asyncio==1.6.0 zipp==1.0.0 absl-py==2.3.1 opt-einsum==3.4.0 scipy==1.15.3 fsspec==2025.9.0 importlib_resources==6.5.2 ml-dtypes==0.5.0 tensorstore==0.1.67 orbax-checkpoint==0.6.4 humanize==4.13.0

pip download zipp==1.0.0 pyparsing==2.4.7 dm-tree==0.1.9 ml-collections==1.1.0 filelock==3.16.1 importlib_metadata==4.6.4 pillow==11.3.0 requests==2.32.3 numpy==1.26.4 wrapt==1.14.2 attrs==21.2.0 absl-py==2.3.1 pyyaml==5.4.1 packaging==21.3 typing-extensions==4.15.0 fsspec==2025.9.0 tqdm==4.67.1 charset-normalizer==3.4.0 certifi==2020.6.20 urllib3==1.26.5 idna==3.3 cached_property==2.0.1 diffusers==0.35.1 hf-xet==1.1.10 huggingface-hub==0.35.3 regex==2025.9.18 safetensors==0.6.2


pip download protobuf==4.21.12 psutil==7.1.0 pyyaml==5.4.1 requests==2.32.3 click==8.0.3 setuptools==69.5.1 typing-extensions==4.15.0 platformdirs==4.3.6 six==1.16.0 idna==3.3 certifi==2020.6.20 charset-normalizer==3.4.0 annotated-types==0.7.0 docker-pycreds==0.4.0 gitdb==4.0.12 gitpython==3.1.45 pydantic==2.12.0 pydantic-core==2.41.1 sentry-sdk==2.40.0 setproctitle==1.3.7 smmap==5.0.2 typing-inspection==0.4.2 urllib3==2.5.0 wandb==0.19.9

pip download requests==2.32.3 fsspec==2025.9.0 google-auth-oauthlib==1.2.2 google-auth==2.41.1 attrs==21.2.0 pyasn1-modules==0.2.1 cachetools==6.2.0 rsa==4.9.1 requests-oauthlib==2.0.0 charset-normalizer==3.4.0 urllib3==2.5.0 idna==3.3 certifi==2020.6.20 typing-extensions==4.15.0 protobuf==4.21.12 oauthlib==3.2.0 pyasn1==0.4.8 aiohappyeyeballs==2.6.1 aiohttp==3.13.0 aiosignal==1.4.0 async-timeout==5.0.1 decorator==5.2.1 frozenlist==1.8.0 gcsfs==2025.9.0 google-api-core==2.25.2 google-cloud-core==2.4.3 google-cloud-storage==3.4.1 google-crc32c==1.7.1 google-resumable-media==2.7.2 googleapis-common-protos==1.70.0 multidict==6.7.0 propcache==0.3.2 proto-plus==1.26.1 yarl==1.22.0

pip download huggingface-hub==0.35.3 h5py==3.14.0 flax==0.10.4 pyyaml==5.4.1 rich==14.1.0 typing-extensions==4.15.0 tensorstore==0.1.67 msgpack==1.1.2 treescope==0.1.10 orbax-checkpoint==0.6.4 optax==0.2.5 scipy==1.15.3 opt-einsum==3.4.0 ml-dtypes==0.5.0 numpy==1.26.4 pillow==11.3.0 requests==2.32.3 tqdm==4.67.1 regex==2025.9.18 packaging==21.3 filelock==3.16.1 hf-xet==1.1.10 fsspec==2025.9.0 charset-normalizer==3.4.0 certifi==2020.6.20 idna==3.3 urllib3==2.5.0 markdown-it-py==4.0.0 pygments==2.19.2 chex==0.1.90 absl-py==2.3.1 humanize==4.13.0 etils==1.13.0 protobuf==4.21.12 nest_asyncio==1.6.0 toolz==1.0.0 mdurl==0.1.2 zipp==1.0.0 importlib_resources==6.5.2 dataclasses==0.6 flaxmodels==0.1.3 lpips-j==0.0.6


rm charset_normalizer-3.4.4-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl idna-3.11-py3-none-any.whl ml_dtypes-0.5.3-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl protobuf-6.33.0-cp39-abi3-manylinux2014_x86_64.whl tensorstore-0.1.78-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl rich-14.2.0-py3-none-any.whl zipp-1.0.0-py2.py3-none-any.whl toolz-1.1.0-py3-none-any.whl numpy-2.2.6-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl humanize-4.14.0-py3-none-any.whl

ls | grep -v pyasn | sed -E 's/[0-9].*//' | awk '
NR == 1 { prev = $0; next }
$0 == prev { print "Error: duplicate prefix \"" $0 "\" on line " NR > "/dev/stderr";}
{ prev = $0 }
END { print "OK: no duplicates." > "/dev/stderr" }
'

cd ..
tar -czvf v5_wheels.tar.gz wheels
gsutil -m cp -r ./v5_wheels.tar.gz gs://kmh-gcp-us-east5/hanhong/v5_wheels.tar.gz