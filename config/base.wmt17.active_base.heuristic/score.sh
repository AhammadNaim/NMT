function test () {
	i=$1
	mkdir -p result_2016/$i
	FAIRSEQ_PATH=/data/fairseq

	python3 translate.py -ckpt result/dden/checkpoints/$i/checkpoint_best_ppl.pth -text ~/test_2016/test.de -ref_text ~/test_2016/test.en --max_batch_size 0 --tokens_per_batch 2000 -max_len 200 > total.out

	cat total.out | grep ^H | cut -d " " -f2- > sys.out
	cat total.out | grep ^T | cut -d " " -f2- > ref.out

	cat sys.out | perl -ple 's{(\S)-(\S)}{$1 ##AT##-##AT## $2}g' > generate.sys
	cat ref.out | perl -ple 's{(\S)-(\S)}{$1 ##AT##-##AT## $2}g' > generate.ref

	python3 $FAIRSEQ_PATH/score.py --sys generate.sys --ref generate.ref > bleu.out
	mv *.out generate.* result_2016/$i/
}

function main () {
	for i in $( seq 1 12 ); do
		test $i
	done
}

main
