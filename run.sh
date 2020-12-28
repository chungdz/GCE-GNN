python preprocess.py --dataset=diginetica
python preprocess.py --dataset=yoochoose

python build_global_graph.py --dataset=diginetica
python build_global_graph.py --dataset=yoochoose1_64
python build_global_graph.py --dataset=yoochoose1_4

CUDA_VISIBLE_DEVICES=1 python pytorch_code/main.py --dataset=yoochoose1_64
CUDA_VISIBLE_DEVICES=0 python pytorch_code/main.py --dataset=diginetica