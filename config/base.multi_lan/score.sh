FAIRSEQ_PATH=/data/fairseq

python3 translate.py -ckpt checkpoints/checkpoint_4.pth -text /data/NMT/data/de-en/news_crawl/test.de-en.de -ref_text /data/NMT/data/de-en/news_crawl/test.de-en.en --max_batch_size 0 --tokens_per_batch 2000 -max_len 200 --src_lan de --tgt_lan en > total.out

cat total.out | grep ^H | cut -d " " -f2- > sys.out
cat total.out | grep ^T | cut -d " " -f2- > ref.out

cat sys.out | perl -ple 's{(\S)-(\S)}{$1 ##AT##-##AT## $2}g' > generate.sys
cat ref.out | perl -ple 's{(\S)-(\S)}{$1 ##AT##-##AT## $2}g' > generate.ref

python3 $FAIRSEQ_PATH/score.py --sys generate.sys --ref generate.ref > bleu.out
