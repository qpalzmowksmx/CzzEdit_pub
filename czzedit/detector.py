from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict, OrderedDict
from statistics import median

from .common import AppError, Config, normalized

RULE_NAMES = {"D1": "웃음 증가", "D2": "채팅 급증", "D3": "집단 반복",
              "D4": "감정 반응", "D5": "참여자 급증"}
EMOTIONS = {
    "surprise": re.compile(r"(?<![가-힣])(와|헐|대박|미쳤다)(?![가-힣])|ㄷ{2,}"),
    "sadness": re.compile(r"아깝다|아쉽다|ㅠ{2,}|ㅜ{2,}"),
}


def reaction_keys(text):
    keys = []
    if re.search(r"ㅋ{2,}|ㅎ{2,}|[😂🤣]", text):
        keys.append("D1")
    for name, pattern in EMOTIONS.items():
        if pattern.search(text):
            keys.append("D4:" + name)
    plain = text.strip(" !?.,~")
    pure_reaction = (re.fullmatch(r"[ㅋㅎ😂🤣]+", plain)
                     or re.fullmatch(r"ㄷ+|ㅠ+|ㅜ+", plain)
                     or plain in ("와", "헐", "대박", "미쳤다", "아깝다", "아쉽다"))
    if text and not pure_reaction:
        keys.append("D3:" + text)
    return keys


class WindowRules:
    """Shared causal window statistics. Memory is bounded by the rolling horizon."""
    def __init__(self, config=None):
        self.config = config or Config()
        self.events = []
        self.times = []
        self.by_id = {}
        self.prepared = {}
        self.gaps = []
        self.cache = OrderedDict()
        self.ignored = {normalized(x) for x in self.config.ignored_phrases}
        self.min_n, self.min_u, self.min_reaction = (6, 3, 3) if self.config.sensitive else (15, 5, 5)

    def add(self, rows):
        if not rows:
            return
        self.events.extend(rows)
        self.events.sort(key=lambda e: (e['time_ms'], e['seq']))
        self.times = [e['time_ms'] for e in self.events]
        for e in rows:
            self.by_id[e['event_id']] = e
            self.prepared[e['event_id']] = normalized(e['content'])
        earliest = min(e['time_ms'] for e in rows)
        self.cache = OrderedDict((k,v) for k,v in self.cache.items() if k[1] <= earliest)

    def prune(self, before):
        cut = bisect.bisect_left(self.times, before)
        for e in self.events[:cut]:
            self.by_id.pop(e['event_id'], None)
            self.prepared.pop(e['event_id'], None)
        del self.events[:cut]
        del self.times[:cut]
        self.gaps = [(a,b) for a,b in self.gaps if b > before]
        self.cache = OrderedDict((k,v) for k,v in self.cache.items() if k[0] >= before)

    def gap(self, start, end):
        if end > start:
            self.gaps.append((start,end))
            self.cache.clear()

    def stats(self, start, end):
        key = (start,end)
        if key in self.cache:
            return self.cache[key]
        invalid = any(start < b and end > a for a,b in self.gaps)
        rows = self.events[bisect.bisect_left(self.times,start):bisect.bisect_left(self.times,end)]
        counts, authors = Counter(), set()
        reactions, evidence = defaultdict(set), defaultdict(list)
        ids = []
        for row in rows:
            text = self.prepared[row['event_id']]
            if not text or text in self.ignored or text.startswith(('!','/')):
                continue
            author = row['author_key']
            if not author:
                invalid = True
                continue
            counts[author] += 1
            if counts[author] > 3:
                continue
            ids.append(row['event_id'])
            authors.add(author)
            for reaction in reaction_keys(text):
                reactions[reaction].add(author)
                evidence[reaction].append(row['event_id'])
        value = {'n':len(ids),'u':len(authors),'valid':not invalid,'ids':ids,
                 'counts':{k:len(v) for k,v in reactions.items()},'evidence':dict(evidence)}
        self.cache[key] = value
        if len(self.cache) > 256:
            self.cache.popitem(last=False)
        return value

    def evaluate(self, t, viewer=None):
        current = self.stats(t - 10000, t)
        # Entire historical bins lie inside [t-940000, t-40000).
        first = max(0, math.ceil((t - 940000) / 10000) * 10000)
        if viewer:
            first = max(first, math.ceil(viewer["epoch_start_ms"] / 10000) * 10000)
        last = math.floor((t - 40000) / 10000) * 10000
        history = [self.stats(b, b + 10000) for b in range(first, last, 10000)]
        history = [h for h in history if h["valid"]]
        ready = len(history) >= 18
        baseline_n = median([h["n"] for h in history]) if ready else None
        baseline_u = median([h["u"] for h in history]) if ready else None
        ratio = current["n"] / max(baseline_n, 3) if ready else None
        mad = median([abs(h["n"] - baseline_n) for h in history]) if ready else None
        z = ((current["n"] - baseline_n) / max(1.4826 * mad, math.sqrt(baseline_n + 1), 1)
             if ready else None)
        denominator = sum(h["u"] for h in history)
        signals, evidence, reaction_ratios = set(), set(), {}
        if current["valid"] and current["n"] >= self.min_n and current["u"] >= self.min_u:
            if ready and ratio >= 3 and z >= 3:
                signals.add("D2")
                evidence.update(current["ids"])
            if ready and current["u"] >= 8 and current["u"] / max(baseline_u, 3) >= 3:
                signals.add("D5")
                evidence.update(current["ids"])
            for key, count in current["counts"].items():
                proportion = count / current["u"]
                threshold = .30 if key.startswith("D4:") else .35
                historical = (sum(h["counts"].get(key, 0) for h in history) / denominator
                              if denominator else 0)
                increased = not ready or (historical is not None and proportion - historical >= .15 - 1e-12)
                if count >= self.min_reaction and proportion >= threshold and increased:
                    signals.add(key)
                    evidence.update(current["evidence"][key])
                    reaction_ratios[key] = proportion
        window = {"t": t, "n": current["n"], "u": current["u"], "ratio": ratio, "z": z,
                  "baseline_n": baseline_n, "baseline_ready": ready,
                  "valid": current["valid"], "signals": sorted(signals),
                  "evidence": sorted(evidence), "reaction_ratios": reaction_ratios}
        if viewer:
            window["viewer_baseline"] = viewer
        strong = ready and current['valid'] and current['n'] >= 30 and current['u'] >= 10 and ratio >= 5 and z >= 5
        return window, strong


class CandidateState:
    """Same grouping/splitting for a finished VOD and an unbounded LIVE stream."""
    def __init__(self, rules):
        self.rules = rules
        self.config = rules.config
        self.active = None
        self.previous = None
        self.previous_baseline = None
        self.serial = 0

    def marker(self, group, end_ms, boundary=None, partial=False):
        start = group['start']
        end = min(boundary or group['end'], end_ms)
        if end <= start:
            return None
        relevant = [w for w in group['windows'] if w['t'] > start and w['t']-10000 < end]
        peak = max(relevant,key=lambda w:(len(w['signals']),w['ratio'] or 0,w['u'],-w['t']))
        keys = sorted({key for w in relevant for key in w['signals']})
        by_id = self.rules.by_id
        evidence = sorted({eid for w in relevant for eid in w['evidence']
                           if eid in by_id and start <= by_id[eid]['time_ms'] < end},
                          key=lambda eid:(by_id[eid]['time_ms'],by_id[eid]['seq']))
        c = self.config
        clip_start = max(0,start-c.reaction_delay_ms-c.pre_roll_ms)
        clip_end = min(end_ms,end-c.reaction_delay_ms+c.post_roll_ms)
        if clip_end < clip_start:
            raise AppError('INVALID_RANGE','반응 보정값 때문에 추천 구간이 유효하지 않습니다.')
        value = {'id':f'm{self.serial+1:04}','group_id':group['group_id'],
                 'reaction_start_ms':start,'reaction_end_ms':end,
                 'peak_ms':min(end-1,max(start,peak['t']-5000)),
                 'clip_start_ms':clip_start,'clip_end_ms':clip_end,'signals':keys,
                 'reasons':sorted({RULE_NAMES[x.split(':')[0]] for x in keys}),
                 'peak_stats':{k:peak[k] for k in ('n','u','ratio','z','baseline_n','reaction_ratios')},
                 'baseline_ready':all(w['baseline_ready'] for w in relevant),
                 'evidence_ids':evidence,'review_state':'미검토',
                 'partial_context':partial or end+c.post_roll_ms>end_ms or start-c.reaction_delay_ms-c.pre_roll_ms<0}
        if 'viewer_baseline' in peak:
            value['viewer_baseline'] = peak['viewer_baseline']
        return value

    def step(self, window, strong, end_ms):
        t = window['t']
        viewer = window.get('viewer_baseline')
        key = (viewer['mode'],viewer['epoch_start_ms']) if viewer else None
        result = []
        def emit(boundary=None, partial=False):
            value = self.marker(self.active,end_ms,boundary,partial)
            if value:
                self.serial += 1
                result.append(value)
        if viewer and self.previous_baseline is not None and key != self.previous_baseline:
            if self.active:
                emit()
                result[-1]['baseline_transition'] = True
                self.active = None
            self.previous = None
        self.previous_baseline = key
        signals = set(window['signals'])
        consecutive = self.previous and self.previous['valid'] and bool(signals.intersection(self.previous['signals']))
        if not window['valid']:
            if self.active:
                emit(partial=True)
                self.active = None
            self.previous = None
            return result
        if self.active and signals and t-10000-self.active['end'] <= 20000:
            self.active['end'] = t
            self.active['windows'].append(window)
        elif signals and (strong or consecutive):
            if self.active:
                emit()
            start = max(0,(self.previous['t'] if consecutive else t)-10000)
            self.active = {'start':start,'end':t,'group_id':f'g{start}',
                           'windows':[self.previous,window] if consecutive else [window]}
        if self.active:
            while self.active['end']-self.active['start'] > 180000:
                boundary = self.active['start']+180000
                emit(boundary=boundary)
                self.active['start'] = boundary
                self.active['windows'] = [w for w in self.active['windows'] if w['t'] > boundary]
            if t >= self.active['end']+max(30000,self.config.post_roll_ms):
                emit()
                self.active = None
        self.previous = window
        return result

    def finish(self, end_ms):
        if not self.active:
            return []
        value = self.marker(self.active,end_ms,partial=True)
        self.active = None
        if not value:
            return []
        self.serial += 1
        return [value]


def analyze(events, end_ms, config=None, gaps=(), progress=None, cancel=None, viewer_samples=None):
    config = config or Config()
    rules = WindowRules(config)
    rules.add([e for e in events if 0 <= e['time_ms'] < end_ms])
    rules.gaps = list(gaps)
    groups = CandidateState(rules)
    from .viewers import Baseline
    viewers = Baseline(viewer_samples) if config.viewer_adaptive and viewer_samples else None
    evaluations = list(range(10000,end_ms+1,5000))
    if end_ms >= 10000 and (not evaluations or evaluations[-1] != end_ms):
        evaluations.append(end_ms)
    markers, windows = [], []
    for index,t in enumerate(evaluations):
        if cancel and cancel.is_set():
            raise AppError('CANCELLED','분석을 중단했습니다. 채팅 원본은 저장되어 있습니다.')
        window,strong = rules.evaluate(t,viewers.at(t) if viewers else None)
        windows.append({k:v for k,v in window.items() if k != 'evidence'})
        markers.extend(groups.step(window,strong,end_ms))
        if progress and (index%100 == 0 or index == len(evaluations)-1):
            progress({'phase':'analyzing','position_ms':t,'duration_ms':end_ms,'marker_count':len(markers)})
    markers.extend(groups.finish(end_ms))
    result = {'markers':markers,'windows':windows,'duration_ms':end_ms,
              'rules_version':'0.1.0','time_basis':'video_position'}
    if viewers:
        result['viewer_epochs'] = viewers.epochs
        result['rules_version'] = '0.2.0-viewer'
    return result
