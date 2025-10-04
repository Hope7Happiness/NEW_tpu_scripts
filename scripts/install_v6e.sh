set -e

(

sudo rm -rf ~/.local

pip install pip==25.1.1

pip install 'setuptools==69.5.1'

pip install numpy==1.26.4 ml-dtypes==0.5.0 jax[tpu]==0.4.37 -f https://storage.googleapis.com/jax-releases/libtpu_releases.html jaxlib==0.4.36 tensorstore==0.1.67 protobuf==4.21.1

pip install orbax-checkpoint==0.6.4 flax==0.10.2

pip install numpy==1.26.4 tensorflow-cpu==2.15.0 'keras<3' tensorflow_datasets

pip install ml-dtypes==0.5.0 --force-reinstall --no-deps

pip install 'torch<=2.4' -i https://download.pytorch.org/whl/cpu torchvision==0.19.0 

pip install pillow clu matplotlib==3.9.2

pip install diffusers dm-tree cached_property ml-collections 'wandb==0.19.9' gcsfs lpips-j==0.0.6

) 2>&1 | grep -oP '(?<=Successfully installed ).*' > env_setup.log

CORRECT_ENV="
pip-25.1.1

setuptools-69.5.1

jax-0.4.37 jaxlib-0.4.36 libtpu-0.0.6 libtpu-nightly-0.1.dev20241010+nightly.cleanup ml-dtypes-0.5.0 numpy-1.26.4 opt_einsum-3.4.0 protobuf-4.21.1 scipy-1.15.3 tensorstore-0.1.67

absl-py-2.3.1 chex-0.1.90 etils-1.13.0 flax-0.10.2 fsspec-2025.9.0 humanize-4.13.0 importlib_resources-6.5.2 markdown-it-py-4.0.0 mdurl-0.1.2 msgpack-1.1.1 nest_asyncio-1.6.0 optax-0.2.5 orbax-checkpoint-0.6.4 pygments-2.19.2 rich-14.1.0 toolz-1.0.0 typing_extensions-4.15.0

MarkupSafe-3.0.3 array_record-0.8.1 astunparse-1.6.3 cachetools-6.2.0 dm-tree-0.1.9 docstring-parser-0.17.0 einops-0.8.1 flatbuffers-25.9.23 gast-0.6.0 google-auth-2.41.1 google-auth-oauthlib-1.2.2 google-pasta-0.2.0 grpcio-1.75.1 h5py-3.14.0 immutabledict-4.2.1 keras-2.15.0 libclang-18.1.1 markdown-3.9 ml-dtypes-0.2.0 promise-2.3 protobuf-4.21.12 psutil-7.1.0 pyarrow-21.0.0 requests-oauthlib-2.0.0 rsa-4.9.1 simple_parsing-0.1.7 tensorboard-2.15.2 tensorboard-data-server-0.7.2 tensorflow-cpu-2.15.0 tensorflow-estimator-2.15.0 tensorflow-io-gcs-filesystem-0.37.1 tensorflow-metadata-1.17.2 tensorflow_datasets-4.9.9 termcolor-3.1.0 toml-0.10.2 tqdm-4.67.1 werkzeug-3.1.3 wrapt-1.14.2

ml-dtypes-0.5.0

mpmath-1.3.0 networkx-3.3 pillow-11.0.0 sympy-1.13.3 torch-2.4.0+cpu torchvision-0.19.0+cpu

clu-0.0.12 contourpy-1.3.2 cycler-0.12.1 fonttools-4.60.1 kiwisolver-1.4.9 matplotlib-3.9.2 ml-collections-1.1.0 python-dateutil-2.9.0.post0

aiohappyeyeballs-2.6.1 aiohttp-3.12.15 aiosignal-1.4.0 annotated-types-0.7.0 async-timeout-5.0.1 cached_property-2.0.1 dataclasses-0.6 decorator-5.2.1 diffusers-0.35.1 docker-pycreds-0.4.0 flaxmodels-0.1.3 frozenlist-1.7.0 gcsfs-2025.9.0 gitdb-4.0.12 gitpython-3.1.45 google-api-core-2.25.2 google-cloud-core-2.4.3 google-cloud-storage-3.4.0 google-crc32c-1.7.1 google-resumable-media-2.7.2 googleapis-common-protos-1.70.0 hf-xet-1.1.10 huggingface-hub-0.35.3 lpips-j-0.0.6 multidict-6.6.4 propcache-0.3.2 proto-plus-1.26.1 pydantic-2.11.9 pydantic-core-2.33.2 regex-2025.9.18 safetensors-0.6.2 sentry-sdk-2.39.0 setproctitle-1.3.7 smmap-5.0.2 typing-inspection-0.4.2 urllib3-2.5.0 wandb-0.19.9 yarl-1.20.1
"

# env_setup.log and CORRECT_ENV can have only newline difference
if diff -Bw <(echo "$CORRECT_ENV") <(cat env_setup.log); then
    echo "Environment setup successful."
else
    echo "Environment setup failed. Output of pip install:"
    echo ===========WRONG============
    cat env_setup.log
    echo ============================
    
    echo "Expected:"
    echo ===========CORRECT==========
    echo "$CORRECT_ENV"
    echo ============================
    exit 1
fi