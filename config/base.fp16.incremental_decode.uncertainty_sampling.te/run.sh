export NGPUS=8
python3 -m torch.distributed.launch --nproc_per_node=$NGPUS train.py 
