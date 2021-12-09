""" beam-search utilities"""
from collections import Counter

from cytoolz import concat
import math

import torch


class _Hypothesis(object):
    def __init__(self, sequence, logprob, hists, attns=[], coverage=None):
        """
        seqence: list of int tokens
        logprob: current log probability
        hists: history of prevous convolution list(n_layers)/
               prev_states and output of lstm ((H, C), out)
        """
        self.sequence = sequence
        self.logprob = logprob
        self.hists = hists
        self.attns = attns  # for unk replacement
        self.coverage = coverage

    def extend_k(self, topk, logprobs, hists, attn=None, diverse=1.0):
        if attn is None:
            attns = []
            coverage = self.coverage
        else:
            attns = self.attns + [attn]
            if self.coverage is None:
                coverage = attn.detach().cpu()
            else:
                coverage = self.coverage + attn.detach().cpu()
        return [_Hypothesis(self.sequence+[t.item()],
                            self.logprob+lp.item()-diverse*i, hists, attns, coverage)
                for i, (t, lp) in enumerate(zip(topk, logprobs))]

    def __lt__(self, other):
        return (other.logprob/len(other.sequence)
                < self.logprob/len(self.sequence))


def init_beam(start, hists):
    """ get a initial beam to start beam search"""

    return [_Hypothesis([start], 0, hists)]


def create_beam(tok, lp, hists):
    """ initailiza a beam with top k token"""
    k = tok.size(0)
    return [_Hypothesis([tok[i].item()], lp[i].item(), hists)
            for i in range(k)]


def pack_beam(hyps, device, use_t5=False):
    """pack a list of hypothesis to decoder input batches"""
    token = torch.LongTensor([h.sequence[-1] for h in hyps])

    if use_t5:
        token = token.to(device)
        states = torch.stack([hyp.hists for hyp in hyps], dim=1)

    else:
        hists = tuple(torch.stack([hyp.hists[i] for hyp in hyps], dim=d)
                      for i, d in enumerate([1, 1, 0]))
        token = token.to(device)
        states = ((hists[0], hists[1]), hists[2])

    return token, states


def next_search_beam(beam, beam_size, finished,
                     end, topk, lp, hists, attn=None, diverse=1.0):
    """generate the next beam(K-best hyps)"""
    topks, lps, hists_list, attns = _unpack_topk(topk, lp, hists, attn)
    hyps_lists = [h.extend_k(topks[i], lps[i],
                             hists_list[i], attns[i], diverse)
                  for i, h in enumerate(beam)]
    hyps = list(concat(hyps_lists))
    finished, beam = _clean_beam(finished, hyps, end, beam_size)

    return finished, beam


def next_search_beam_cnn(beam, beam_size, finished,
                     end, topk, lp, hists, attn=None, diverse=1.0):
    """generate the next beam(K-best hyps)"""
    topks, lps, hists_list, attns = _unpack_topk(topk, lp, hists, attn)
    hyps_lists = [h.extend_k(topks[i], lps[i],
                             hists_list[i], attns[i], diverse)
                  for i, h in enumerate(beam)]
    hyps = list(concat(hyps_lists))
    finished, beam = _clean_beam_cnn(finished, hyps, end, beam_size)

    return finished, beam


def best_sequence(finished, beam=None):
    """ return the sequence with the highest prob(normalized by length)"""
    if beam is None:  # not empty
        best_beam = finished[0]
    else:
        if finished and beam[0] < finished[0]:
            best_beam = finished[0]
        else:
            best_beam = beam[0]

    best_seq = best_beam.sequence[1:]
    if best_beam.attns:
        return best_seq, best_beam.attns
    else:
        return best_seq


def _unpack_topk(topk, lp, hists, attn=None):
    """unpack the decoder output"""
    beam, _ = topk.size()
    topks = [t for t in topk]
    lps = [l for l in lp]

    if len(hists) == 3:
        k_hists = [(hists[0][:, i, :], hists[1][:, i, :], hists[2][i, :])
                   for i in range(beam)]
    else:
        k_hists = [hists[:, i, :] for i in range(beam)]

    if attn is None:
        return topks, lps, k_hists
    else:
        attns = [attn[i] for i in range(beam)]
        return topks, lps, k_hists, attns

def length_wu(cur_len, alpha=0.):
    """GNMT length re-ranking score.
    See "Google's Neural Machine Translation System" :cite:`wu2016google`.
    """
    return ((5 + cur_len) / 6.0) ** alpha


def coverage_summary(cov, beta=0.):
    """Our summary penalty."""
    penalty = torch.max(cov, cov.clone().fill_(1.0)).sum(-1)
    penalty -= cov.size(-1)
    return beta * penalty

def _clean_beam(finished, beam, end_tok, beam_size, remove_tri=True):
    """ remove completed sequence from beam """
    new_beam = []
    # for h in sorted(beam, reverse=True,
    #                 key=lambda h: h.logprob/len(h.sequence)):
    # for h in sorted(beam, reverse=True,
    #                 key=lambda h: h.logprob / length_wu(len(h.sequence), alpha=0.9) - coverage_summary(h.coverage, beta=5)):
    for h in sorted(beam, reverse=True,
                    key=lambda h: h.logprob / length_wu(len(h.sequence), alpha=0.9)):
        if remove_tri and _has_repeat_tri(h.sequence):
            h.logprob = -1e9
        if h.sequence[-1] == end_tok:
            finished_hyp = _Hypothesis(h.sequence[:-1], # remove EOS
                                       h.logprob, h.hists, h.attns, h.coverage)
            finished.append(finished_hyp)
        else:
            new_beam.append(h)
        if len(new_beam) == beam_size:
            break
    else:
        # ensure beam size
        while len(new_beam) < beam_size:
            new_beam.append(new_beam[0])

    finished = sorted(finished, reverse=True,
                      key=lambda h: h.logprob/len(h.sequence))
    return finished, new_beam

def _clean_beam_cnn(finished, beam, end_tok, beam_size, remove_tri=True):
    """ remove completed sequence from beam """
    new_beam = []
    # for h in sorted(beam, reverse=True,
    #                 key=lambda h: h.logprob/len(h.sequence)):
    # for h in sorted(beam, reverse=True,
    #                 key=lambda h: h.logprob / length_wu(len(h.sequence), alpha=0.9) - coverage_summary(h.coverage, beta=5)):
    for h in sorted(beam, reverse=True,
                    key=lambda h: h.logprob / len(h.sequence)):
        if remove_tri and _has_repeat_tri(h.sequence):
            h.logprob = -1e9
            continue
        if h.sequence[-1] == end_tok:
            finished_hyp = _Hypothesis(h.sequence[:-1], # remove EOS
                                       h.logprob, h.hists, h.attns)
            finished.append(finished_hyp)
        else:
            new_beam.append(h)
        if len(new_beam) == beam_size:
            break
    else:
        # ensure beam size
        while len(new_beam) < beam_size:
            new_beam.append(new_beam[0])

    finished = sorted(finished, reverse=True,
                      key=lambda h: h.logprob/len(h.sequence))
    return finished, new_beam


def _has_repeat_tri(grams):
    tri_grams = [tuple(grams[i:i+3]) for i in range(len(grams)-2)]
    cnt = Counter(tri_grams)
    return not all((cnt[g] <= 1 for g in cnt))
