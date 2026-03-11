gsutil -m cp -r gs://kmh-gcp-us-east5/hanhong/v5_wheels_new.tar.gz ./wheels.tar.gz
tar -xvf wheels.tar.gz

cd wheels

pip download "jax[tpu]==0.6.2" -f https://storage.googleapis.com/jax-releases/libtpu_releases.html
pip download jaxlib==0.6.2 timm
pip download orbax-checkpoint==0.11.32 pandas
pip download jaxtyping==0.3.7
pip download sentencepiece webdataset==1.0.2
pip download protobuf==3.20.3

rm absl_py-2.3.1-py3-none-any.whl certifi-2026.1.4-py3-none-any.whl filelock-3.20.3-py3-none-any.whl fsspec-2026.1.0-py3-none-any.whl hf_xet-1.2.0-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl huggingface_hub-0.36.0-py3-none-any.whl jax-0.4.34-py3-none-any.whl  jaxlib-0.4.34-cp310-cp310-manylinux2014_x86_64.whl libtpu-0.0.6-py3-none-manylinux_2_27_x86_64.whl numpy-1.26.4-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl nvidia_cublas_cu12-12.1.3.1-py3-none-manylinux1_x86_64.whl nvidia_cuda_cupti_cu12-12.1.105-py3-none-manylinux1_x86_64.whl nvidia_cuda_nvrtc_cu12-12.1.105-py3-none-manylinux1_x86_64.whl nvidia_cuda_runtime_cu12-12.1.105-py3-none-manylinux1_x86_64.whl nvidia_cudnn_cu12-9.1.0.70-py3-none-manylinux2014_x86_64.whl nvidia_cufft_cu12-11.0.2.54-py3-none-manylinux1_x86_64.whl nvidia_curand_cu12-10.3.2.106-py3-none-manylinux1_x86_64.whl nvidia_cusolver_cu12-11.4.5.107-py3-none-manylinux1_x86_64.whl nvidia_cusparse_cu12-12.1.0.106-py3-none-manylinux1_x86_64.whl nvidia_nccl_cu12-2.20.5-py3-none-manylinux2014_x86_64.whl nvidia_nvjitlink_cu12-12.9.86-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl nvidia_nvtx_cu12-12.1.105-py3-none-manylinux1_x86_64.whl orbax_checkpoint-0.6.4-py3-none-any.whl pillow-12.1.0-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl protobuf-7.34.0-cp310-abi3-manylinux2014_x86_64.whl protobuf-6.33.4-cp39-abi3-manylinux2014_x86_64.whl psutil-7.2.1-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl rich-14.2.0-py3-none-any.whl torch-2.4.0-cp310-cp310-manylinux1_x86_64.whl torchvision-0.19.0-cp310-cp310-manylinux1_x86_64.whl tqdm-4.67.1-py3-none-any.whl triton-3.0.0-1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl


cd ..
tar -czvf v5_wheels.tar.gz wheels
gsutil -m cp -r ./v5_wheels.tar.gz gs://kmh-gcp-us-east5/hanhong/v5_wheels_xin.tar.gz
