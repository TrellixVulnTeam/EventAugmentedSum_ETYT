""" train the abstractor"""
from graph_augmented_sum.training import get_basic_grad_fn, basic_validate
from graph_augmented_sum.training import BasicPipeline, BasicTrainer, MultiTaskPipeline, MultiTaskTrainer
import argparse
import json
import os, re
from os.path import join, exists
import pickle as pkl

from cytoolz import compose, concat

import torch
from torch import optim
from torch.nn import functional as F
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader, Dataset

from graph_augmented_sum.model.copy_summ import CopySumm
from graph_augmented_sum.model.copy_summ_multiencoder import CopySummGAT, CopySummParagraph
from graph_augmented_sum.model.util import sequence_loss

from graph_augmented_sum.data.batcher import coll_fn, prepro_fn
from graph_augmented_sum.data.batcher import prepro_fn_copy_bert, convert_batch_copy_bert, batchify_fn_copy_bert
from graph_augmented_sum.data.batcher import convert_batch_copy, batchify_fn_copy
from graph_augmented_sum.data.batcher import BucketedGenerater
from graph_augmented_sum.data.abs_batcher import convert_batch_gat, batchify_fn_gat, prepro_fn_gat, coll_fn_gat
from graph_augmented_sum.data.abs_batcher import convert_batch_gat_bert, batchify_fn_gat_bert, prepro_fn_gat_bert
from graph_augmented_sum.training import multitask_validate

from graph_augmented_sum.utils import PAD, UNK, START, END
from graph_augmented_sum.utils import make_vocab, make_embedding
from transformers import RobertaTokenizer, BertTokenizer
import pickle

# NOTE: bucket size too large may sacrifice randomness,
#       to low may increase # of PAD tokens
BUCKET_SIZE = 6400

try:
    DATA_DIR = os.environ['DATA']
except KeyError:
    print('please use environment variable to specify data directories')


class PubmedDataset(Dataset):
    """ single article sentence -> single abstract sentence
    (dataset created by greedily matching ROUGE)
    """

    def __init__(self, split: str, path: str):
        self._data_path = join(path, '{}.txt'.format(split))

        self._js_data = []
        with open(self._data_path, 'rb') as f:
            for line in f:
                self._js_data.append(json.loads(line))

    def __len__(self):
        return len(self._js_data)

    def __getitem__(self, i):
        js_i = self._js_data[i]
        art_sents, abs_sents, article_id = (js_i['article_text'], js_i['abstract_text'], js_i['article_id'])
        abs_sents = [' '.join(abs_sents)]
        return art_sents, abs_sents, article_id


def get_bert_align_dict(filename='preprocessing/bertalign-base.pkl'):
    with open(filename, 'rb') as f:
        bert_dict = pickle.load(f)
    return bert_dict


def configure_net(vocab_size, emb_dim,
                  n_hidden, bidirectional, n_layer, load_from=None, bert=False, max_art=800):
    net_args = {}
    net_args['vocab_size'] = vocab_size
    net_args['emb_dim'] = emb_dim
    net_args['n_hidden'] = n_hidden
    net_args['bidirectional'] = bidirectional
    net_args['n_layer'] = n_layer
    net_args['bert'] = bert
    net_args['bert_length'] = max_art

    net = CopySumm(**net_args)
    if load_from is not None:
        abs_ckpt = load_best_ckpt(load_from)
        net.load_state_dict(abs_ckpt)

    return net, net_args


def configure_net_gat(vocab_size, emb_dim,
                      n_hidden, bidirectional, n_layer, load_from=None, gat_args={},
                      adj_type='no_edge', mask_type='none',
                      feed_gold=False, graph_layer_num=1, feature=[], subgraph=False, hierarchical_attn=False,
                      bert=False, bert_length=512, use_t5=False
                      ):
    net_args = {}
    net_args['vocab_size'] = vocab_size
    net_args['emb_dim'] = emb_dim
    net_args['side_dim'] = n_hidden
    net_args['n_hidden'] = n_hidden
    net_args['bidirectional'] = bidirectional
    net_args['n_layer'] = n_layer
    net_args['gat_args'] = gat_args
    net_args['feed_gold'] = feed_gold
    net_args['mask_type'] = mask_type
    net_args['adj_type'] = adj_type
    net_args['graph_layer_num'] = graph_layer_num
    net_args['feature_banks'] = feature
    net_args['bert'] = bert
    net_args['bert_length'] = bert_length
    if subgraph:
        net_args['hierarchical_attn'] = hierarchical_attn
    else:
        net_args['use_t5'] = use_t5

    if subgraph:
        net = CopySummParagraph(**net_args)
    else:
        net = CopySummGAT(**net_args)
    if load_from is not None:
        abs_ckpt = load_best_ckpt(load_from)
        net.load_state_dict(abs_ckpt)

    return net, net_args


def load_best_ckpt(model_dir, reverse=False):
    """ reverse=False->loss, reverse=True->reward/score"""
    ckpts = os.listdir(join(model_dir, 'ckpt'))
    ckpt_matcher = re.compile('^ckpt-.*-[0-9]*')
    ckpts = sorted([c for c in ckpts if ckpt_matcher.match(c)],
                   key=lambda c: float(c.split('-')[1]), reverse=reverse)
    print('loading checkpoint {}...'.format(ckpts[0]))
    ckpt = torch.load(
        join(model_dir, 'ckpt/{}'.format(ckpts[0])), map_location=lambda storage, loc: storage
    )['state_dict']
    return ckpt


def configure_training(opt, lr, clip_grad, lr_decay, batch_size, bert):
    """ supports Adam optimizer only"""
    assert opt in ['adam', 'adagrad']
    opt_kwargs = {}
    opt_kwargs['lr'] = lr

    train_params = {}
    if opt == 'adagrad':
        opt_kwargs['initial_accumulator_value'] = 0.1
    train_params['optimizer'] = (opt, opt_kwargs)
    train_params['clip_grad_norm'] = clip_grad
    train_params['batch_size'] = batch_size
    train_params['lr_decay'] = lr_decay
    if bert:
        PAD = 1
    else:
        PAD = 0
    nll = lambda logit, target: F.nll_loss(logit, target, reduce=False)

    def criterion(logits, targets):
        return sequence_loss(logits, targets, nll, pad_idx=PAD)

    print('pad id:', PAD)
    return criterion, train_params


def configure_training_multitask(opt, lr, clip_grad, lr_decay, batch_size, mask_type, bert):
    """ supports Adam optimizer only"""
    assert opt in ['adam', 'adagrad']
    opt_kwargs = {}
    opt_kwargs['lr'] = lr

    train_params = {}
    if opt == 'adagrad':
        opt_kwargs['initial_accumulator_value'] = 0.1
    train_params['optimizer'] = (opt, opt_kwargs)
    train_params['clip_grad_norm'] = clip_grad
    train_params['batch_size'] = batch_size
    train_params['lr_decay'] = lr_decay

    if bert:
        PAD = 1
    nll = lambda logit, target: F.nll_loss(logit, target, reduce=False)

    bce = lambda logit, target: F.binary_cross_entropy(logit, target, reduce=False)

    def criterion(logits1, logits2, targets1, targets2):
        aux_loss = None
        for logit in logits2:
            if aux_loss is None:
                aux_loss = sequence_loss(logit, targets2, bce, pad_idx=-1, if_aux=True, fp16=False).mean()
            else:
                aux_loss += sequence_loss(logit, targets2, bce, pad_idx=-1, if_aux=True, fp16=False).mean()
        return sequence_loss(logits1, targets1, nll, pad_idx=PAD).mean(), aux_loss

    print('pad id:', PAD)
    return criterion, train_params


def build_batchers_bert(cuda, debug, bert_model):

    tokenizer = RobertaTokenizer.from_pretrained(bert_model)

    # mul didn't receive enough arguments to evaluate (missing batch)
    # so it waits, returning a
    # partially evaluated function 'prepro'
    prepro = prepro_fn_copy_bert(tokenizer, args.max_art, args.max_abs)

    def sort_key(sample):
        src, target = sample[0], sample[1]
        return len(target), len(src)

    batchify = compose(
        batchify_fn_copy_bert(tokenizer, cuda=cuda),
        convert_batch_copy_bert(tokenizer, args.max_art)
    )

    # coll_fn is needed to filter out too short abstracts (<4) and articles (<5)
    train_loader = DataLoader(
        PubmedDataset('play', DATA_DIR), batch_size=BUCKET_SIZE,
        shuffle=not debug,
        num_workers=4 if cuda and not debug else 0,
        collate_fn=coll_fn
    )
    train_batcher = BucketedGenerater(train_loader, prepro, sort_key, batchify,
                                      single_run=False, fork=not debug)
    val_loader = DataLoader(
        PubmedDataset('play', DATA_DIR), batch_size=BUCKET_SIZE,
        shuffle=False, num_workers=4 if cuda and not debug else 0,
        collate_fn=coll_fn
    )
    val_batcher = BucketedGenerater(val_loader, prepro, sort_key, batchify,
                                    single_run=True, fork=not debug)

    return train_batcher, val_batcher, tokenizer.encoder


def main(args):
    # create data batcher, vocabulary
    # batcher
    if args.bert:
        import logging
        logging.basicConfig(level=logging.ERROR)

    train_batcher, val_batcher, word2id = build_batchers_bert(args.cuda, args.debug, args.bertmodel)

    net, net_args = configure_net(len(word2id), args.emb_dim,
                                  args.n_hidden, args.bi, args.n_layer, args.load_from, args.bert, args.max_art)

    criterion, train_params = configure_training(
        'adam', args.lr, args.clip, args.decay, args.batch, args.bert
    )

    # save experiment setting
    if not exists(args.path):
        os.makedirs(args.path)
    with open(join(args.path, 'vocab.pkl'), 'wb') as f:
        pkl.dump(word2id, f, pkl.HIGHEST_PROTOCOL)
    meta = {}
    meta['net'] = 'base_abstractor'
    meta['net_args'] = net_args
    meta['traing_params'] = train_params
    with open(join(args.path, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=4)

    # prepare trainer
    if args.cuda:
        net = net.cuda()

    if 'soft' in args.mask_type and args.gat:
        val_fn = multitask_validate(net, criterion)
    else:
        val_fn = basic_validate(net, criterion)
    grad_fn = get_basic_grad_fn(net, args.clip)
    print(net._embedding.weight.requires_grad)

    optimizer = optim.AdamW(net.parameters(), **train_params['optimizer'][1])

    # optimizer = optim.Adagrad(net.parameters(), **train_params['optimizer'][1])
    scheduler = ReduceLROnPlateau(optimizer, 'min', verbose=True,
                                  factor=args.decay, min_lr=0,
                                  patience=args.lr_p)

    pipeline = BasicPipeline(meta['net'], net,
                             train_batcher, val_batcher, args.batch, val_fn,
                             criterion, optimizer, grad_fn)
    trainer = BasicTrainer(pipeline, args.path,
                           args.ckpt_freq, args.patience, scheduler)

    print('start training with the following hyper-parameters:')
    print(meta)
    trainer.train()


if __name__ == '__main__':
    if torch.cuda.is_available():
        torch.cuda.set_device(0)
    parser = argparse.ArgumentParser(
        description='training of the abstractor (ML)'
    )
    parser.add_argument('--path', required=True, help='root of the model')
    parser.add_argument('--key', type=str, default='extracted_combine', help='constructed sentences')

    parser.add_argument('--vsize', type=int, action='store', default=50000,
                        help='vocabulary size')
    parser.add_argument('--emb_dim', type=int, action='store', default=128,
                        help='the dimension of word embedding')
    parser.add_argument('--w2v', action='store',
                        help='use pretrained word2vec embedding')
    parser.add_argument('--n_hidden', type=int, action='store', default=256,
                        help='the number of hidden units of LSTM')
    parser.add_argument('--n_layer', type=int, action='store', default=1,
                        help='the number of layers of LSTM')

    parser.add_argument('--docgraph', action='store_true', help='uses gat encoder')
    parser.add_argument('--paragraph', action='store_true', help='encode topic flow')
    parser.add_argument('--mask_type', action='store', default='soft', type=str,
                        help='none, encoder, soft')
    parser.add_argument('--graph_layer', type=int, default=1, help='graph layer number')
    parser.add_argument('--adj_type', action='store', default='edge_as_node', type=str,
                        help='concat_triple, edge_up, edge_down, no_edge, edge_as_node')
    parser.add_argument('--gold_key', action='store', default='summary_worthy', type=str,
                        help='attention type')
    parser.add_argument('--feat', action='append', default=['node_freq'])
    parser.add_argument('--bert', action='store_true', help='use bert!')
    parser.add_argument('--bertmodel', action='store', type=str, default='roberta-base',
                        help='roberta-base')
    parser.add_argument('--use_t5', action='store', type=bool, default=False,
                        help='use t5 encoder and decoder instead of LSTM networks')

    # length limit
    parser.add_argument('--max_art', type=int, action='store', default=1024,
                        help='maximun words in a single article sentence')
    parser.add_argument('--max_abs', type=int, action='store', default=150,
                        help='maximun words in a single abstract sentence')
    # training options
    parser.add_argument('--lr', type=float, action='store', default=1e-3,
                        help='learning rate')
    parser.add_argument('--decay', type=float, action='store', default=0.5,
                        help='learning rate decay ratio')
    parser.add_argument('--lr_p', type=int, action='store', default=0,
                        help='patience for learning rate decay')
    parser.add_argument('--clip', type=float, action='store', default=2.0,
                        help='gradient clipping')
    parser.add_argument('--batch', type=int, action='store', default=32,
                        help='the training batch size')
    parser.add_argument('--num_worker', type=int, action='store', default=4,
                        help='cpu num using for dataloader')
    parser.add_argument(
        '--ckpt_freq', type=int, action='store', default=9000,
        help='number of update steps for checkpoint and validation'
    )
    parser.add_argument('--patience', type=int, action='store', default=5,
                        help='patience for early stopping')

    parser.add_argument('--debug', action='store_true',
                        help='run in debugging mode')
    parser.add_argument('--no-cuda', action='store_true',
                        help='disable GPU training')
    parser.add_argument('--load_from', type=str, default=None,
                        help='disable GPU training')
    parser.add_argument('--gpu_id', type=int, default=0, help='gpu id')
    args = parser.parse_args()
    if args.debug:
        BUCKET_SIZE = 64
    args.bi = True
    if args.docgraph or args.paragraph:
        args.gat = True
    else:
        args.gat = False
    if args.paragraph:
        args.topic_flow_model = True
    else:
        args.topic_flow_model = False

    args.cuda = torch.cuda.is_available() and not args.no_cuda
    if args.cuda:
        torch.cuda.set_device(args.gpu_id)

    args.n_gpu = 1

    main(args)
