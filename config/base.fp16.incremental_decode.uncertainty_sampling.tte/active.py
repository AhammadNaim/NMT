from common import config
from dataset import Dataset
from translate import load_model
import argparse
import model
import numpy as np
import os
import torch
import torch.nn.functional as F
from torch.autograd import Variable
from utils import subsequent_mask, remove_bpe, remove_special_tok


def gen_batch2str(src, generated, gen_len, src_vocab, tgt_vocab):
    generated = generated.cpu().numpy().tolist()
    gen_len = gen_len.cpu().numpy().tolist()
    src = src.cpu().numpy().tolist()
    translated = []
    for i, l in enumerate(generated):
        l = l[:gen_len[i]]
        sys_sent = " ".join([tgt_vocab.itos[tok] for tok in l])
        src_sent = " ".join([src_vocab.itos[tok] for tok in src[i]])
        sys_sent = remove_special_tok(remove_bpe(sys_sent))
        src_sent = remove_special_tok(remove_bpe(src_sent))
        translated.append("S: " + src_sent)
        translated.append("H: " + sys_sent)
    return translated


def _get_scores(args, net, active_func, src, src_mask, indices, src_vocab, tgt_vocab):
    net.eval()
    max_len = args.max_len
    average_by_length = (active_func != "tte")
    result = []
    print("src size :{}".format(src.size()))
    with torch.no_grad():
        bsz = src.size(0)
        enc_out = net.encode(src=src, src_mask=src_mask)
        generated = src.new(bsz, max_len)
        generated.fill_(tgt_vocab.stoi[config.PAD])
        generated[:, 0].fill_(tgt_vocab.stoi[config.BOS])
        generated = generated.long()
        
        cur_len = 1
        gen_len = src.new_ones(bsz).long()
        unfinished_sents = src.new_ones(bsz).long()
        query_scores = src.new_zeros(bsz).float()

        cache = {'cur_len':cur_len - 1}

        while cur_len < max_len:
            x = generated[:, cur_len - 1].unsqueeze(-1)
            tgt_mask = ( generated[:, :cur_len] != tgt_vocab.stoi[config.PAD] ).unsqueeze(-2)
            tgt_mask = tgt_mask & Variable(
                    subsequent_mask(cur_len).type_as(tgt_mask.data)
                    )

            logit = net.decode(
                    enc_out, src_mask, x,
                    tgt_mask[:, cur_len-1, :].unsqueeze(-2), cache
                    )
            scores = net.generator(logit).exp().squeeze().data
            
            # Calculate activation function value
            # The smaller query score is, the more uncertain model is about the sentence
            if active_func == "lc":
                q_scores, _ = torch.topk(scores, 1, dim=-1)
                q_scores = -(1.0 - q_scores).squeeze()
            elif active_func == "margin":
                q_scores, _ = torch.topk(scores, 2, dim=-1)
                q_scores = q_scores[:, 0] - q_scores[:, 1]
            elif active_func == "te" or active_func == "tte":
                q_scores = -torch.distributions.categorical.Categorical(probs=scores).entropy()
            q_scores = q_scores.view(bsz)
            
            query_scores = query_scores + unfinished_sents.float() * q_scores
            
            next_words = torch.topk(scores, 1)[1].squeeze()

            next_words = next_words.view(bsz)
            generated[:, cur_len] = next_words * unfinished_sents + tgt_vocab.stoi[config.PAD] * (1 - unfinished_sents)
            gen_len.add_(unfinished_sents)
            unfinished_sents.mul_(next_words.ne(tgt_vocab.stoi[config.EOS]).long())
            cur_len = cur_len + 1
            cache['cur_len'] = cur_len - 1

            if unfinished_sents.max() == 0:
                break
        
        if cur_len == max_len:
            generated[:, -1].masked_fill_(unfinished_sents.bool(), tgt_vocab.stoi[config.EOS])
        
        translated = gen_batch2str(src, generated[:, :cur_len], gen_len, src_vocab, tgt_vocab)
        for new_sent in translated:
            print(new_sent)

        if average_by_length:
            query_scores = query_scores / gen_len.float()
        query_scores = query_scores.cpu().numpy().tolist()
        indices = indices.tolist()
        assert len(query_scores) == len(indices)
        print("Indices lengths is {}".format(len(indices)))
        for q_s, idx in zip(query_scores, indices):
            result.append((q_s, idx))
    return result


def split_batch(src, indices, max_batch_size=800):
    bsz = src.size(0)
    if bsz <= max_batch_size:
        splited = False
        return src, indices, splited
    else:
        src = torch.split(src, max_batch_size)
        indices_chunks = []
        splited = True
        for chunk in src:
            bsz = chunk.size(0)
            indices_chunks.append(indices[:bsz])
            indices = indices[bsz:]
        return src, indices_chunks, splited


def get_scores(args, net, active_func, infer_dataiter, src_vocab, tgt_vocab):
    results = []
    for (src, indices) in infer_dataiter:
        src, indices, splited = split_batch(src, indices)

        if splited:
            assert len(src) == len(indices)
            for src_chunk, indices_chunk in zip(src, indices):
                assert src_chunk.size(0) == len(indices_chunk)
                src_mask = (src_chunk != src_vocab.stoi[config.PAD]).unsqueeze(-2)
                if args.use_cuda:
                    src_chunk, src_mask = src_chunk.cuda(), src_mask.cuda()
                result = _get_scores(args, net, active_func, src_chunk, src_mask, indices_chunk, src_vocab, tgt_vocab)
                results.extend(result)    
        else:
            src_mask = (src != src_vocab.stoi[config.PAD]).unsqueeze(-2)
            if args.use_cuda:
                src, src_mask = src.cuda(), src_mask.cuda()
            result = _get_scores(args, net, active_func, src, src_mask, indices, src_vocab, tgt_vocab)
            results.extend(result)    

    return results


def query_instances(args, unlabeled_dataset, active_func="random", tok_budget=None):
    # lc stands for least confident
    # te stands for token entropy
    # tte stands for total token entropy
    assert active_func in ["random", "longest", "shortest", "lc", "margin", "te", "tte"]
    assert isinstance(tok_budget, int)

    # lengths represents number of tokens, so BPE should be removed
    lengths = np.array([len(remove_special_tok(remove_bpe(s)).split()) for s in unlabeled_dataset])
    total_num = sum(lengths)
    if total_num < tok_budget:
        tok_budget = total_num
    
    # Preparations before querying instances
    if active_func in ["lc", "margin", "te", "tte"]:
        # Reloading network parameters
        args.use_cuda = ( args.no_cuda == False ) and torch.cuda.is_available()
        net, _ = model.get()

        assert os.path.exists(args.checkpoint)
        net, src_vocab, tgt_vocab = load_model(args.checkpoint, net)

        if args.use_cuda:
            net = net.cuda()
        
        # Initialize inference dataset (Unlabeled dataset)
        infer_dataset = Dataset(unlabeled_dataset, src_vocab)
        if args.batch_size is not None:
            infer_dataset.BATCH_SIZE = args.batch_size
        if args.max_batch_size is not None:
            infer_dataset.max_batch_size = args.max_batch_size
        if args.tokens_per_batch is not None:
            infer_dataset.tokens_per_batch = args.tokens_per_batch

        infer_dataiter = iter(infer_dataset.get_iterator(
            shuffle=True, group_by_size=True, include_indices=True
            ))

    # Start ranking unlabeled dataset
    indices = np.arange(len(unlabeled_dataset))
    if active_func == "random":
        np.random.shuffle(indices)
    elif active_func == "longest":
        indices = indices[np.argsort(-lengths[indices])]
    elif active_func == "shortest":
        indices = indices[np.argsort(lengths[indices])]
    elif active_func in ["lc", "margin", "te", "tte"]:
        result = get_scores(args, net, active_func, infer_dataiter, src_vocab, tgt_vocab)
        result = sorted(result, key=lambda item:item[0])
        indices = [item[1] for item in result]
        indices = np.array(indices).astype('int')

    include = np.cumsum(lengths[indices]) <= tok_budget
    include = indices[include]
    return [unlabeled_dataset[idx] for idx in include], include


def label_queries(queries, oracle):
    assert isinstance(queries, np.ndarray)
    queries = queries.astype('int').tolist()
    return [oracle[idx] for idx in queries]


def change_datasets(unlabeled_dataset, labeled_dataset, labeled_queries, query_indices):
    assert len(labeled_queries[0]) == len(query_indices)
    assert len(labeled_queries[1]) == len(labeled_queries[1])
    unlabeled_dataset = [unlabeled_dataset[idx] for idx in range(len(unlabeled_dataset)) if idx not in query_indices]
    labeled_dataset[0].extend(labeled_queries[0])
    labeled_dataset[1].extend(labeled_queries[1])

    return unlabeled_dataset, labeled_dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
            "-U", "--unlabeled_dataset", type=str,
            help="where to read unlabelded dataset", required=True
            )
    parser.add_argument(
            "-L", "--labeled_dataset", type=str,
            help="where to read labeled dataset, split by comma, e.g. l.de,l.en", required=True
            )
    parser.add_argument(
            "--oracle", type=str,
            help="where to read oracle dataset",
            required=True
            )
    parser.add_argument(
            "-tb", "--tok_budget", type=int,
            help="Token budget", required=True
            )
    parser.add_argument(
            "-OU", "--output_unlabeled_dataset", type=str,
            help="path to store new unlabeled dataset", required=True
            )
    parser.add_argument(
            "-OL", "--output_labeled_dataset", type=str,
            help="path to store new labeled dataset", required=True
            )
    parser.add_argument(
            "-OO", "--output_oracle", type=str,
            help="path to oracle", required=True
            )
    parser.add_argument(
            "-a", "--active_func", type=str,
            help="Active query function type", required=True
            )
    parser.add_argument(
            '-ckpt', '--checkpoint', type=str,
            help="Checkpoint path to reload network parameters"
            )
    parser.add_argument(
            '-max_len', type=int, default=250,
            help="Maximum length for generating translations"
            )
    parser.add_argument(
            '-no_cuda', action="store_true",
            help="Use cpu to do translation"
            )
    parser.add_argument(
            '--batch_size', type=int, default=None,
            help="Batch size for generating translations"
            )
    parser.add_argument(
            '--max_batch_size', type=int, default=None,
            help="Maximum batch size if tokens_per_batch is not None"
            )
    parser.add_argument(
            '--tokens_per_batch', type=int, default=None,
            help="Maximum number of tokens in a batch when generating translations"
            )
    args = parser.parse_args()

    # Read labeled and unlabeled datasets
    f = open(args.unlabeled_dataset, 'r')
    unlabeled_dataset = f.read().split("\n")[:-1]
    f.close()

    src_labeled_dataset, tgt_labeled_dataset = args.labeled_dataset.split(",")
    labeled_dataset = []
    f = open(src_labeled_dataset, 'r')
    labeled_dataset.append(f.read().split("\n")[:-1])
    f.close()

    f = open(tgt_labeled_dataset, 'r')
    labeled_dataset.append(f.read().split("\n")[:-1])
    f.close()

    # Read oracle
    f = open(args.oracle, "r")
    oracle = f.read().split("\n")[:-1]
    assert len(oracle) == len(unlabeled_dataset)

    # Query instances
    queries, query_indices = query_instances(args, unlabeled_dataset, args.active_func, args.tok_budget)

    # Label instances
    labeled_queries = [queries]
    labeled_queries.append( label_queries(query_indices, oracle) )

    # Change datasets
    unlabeled_dataset, labeled_dataset = change_datasets(
            unlabeled_dataset, labeled_dataset, labeled_queries, query_indices
            )
    
    oracle = [oracle[idx] for idx in range(len(oracle)) if idx not in query_indices]
    # Store new labeled, unlabeled, oracle dataset
    f = open(args.output_unlabeled_dataset, 'w')
    f.write("\n".join(unlabeled_dataset) + "\n")
    f.close()

    output_src_labeled_dataset, output_tgt_labeled_dataset = args.output_labeled_dataset.split(",")
    f = open(output_src_labeled_dataset, 'w')
    f.write("\n".join(labeled_dataset[0]) + "\n")
    f.close()

    f = open(output_tgt_labeled_dataset, 'w')
    f.write("\n".join(labeled_dataset[1]) + "\n")
    f.close()

    f = open(args.output_oracle, 'w')
    f.write("\n".join(oracle) + "\n")
    f.close()


if __name__ == "__main__":
    main()
