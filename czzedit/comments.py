"""CHZZK website comment adapter (not the official LIVE chat API)."""
import hashlib
import hmac
import json
import re
from urllib.parse import urlencode
from .common import AppError, video_id
from .transport import PublicTransport, BrowserTransport, Transport, HEADERS
from . import records

BASE = 'https://apis.naver.com/nng_main/nng_comment_api/v1'
TIME = re.compile(r'(?<![\d:\-])(?:\d+:\d{2}(?::\d{2})?|(?:\d+시간\s*)?\d+분\s*\d+초)(?![\d:])')


def milliseconds(text):
    if ':' in text:
        parts = list(map(int, text.split(':')))
        if parts[-1] >= 60 or len(parts) == 3 and parts[1] >= 60:
            return None
        return (parts[0]*60+parts[1])*1000 if len(parts) == 2 else (parts[0]*3600+parts[1]*60+parts[2])*1000
    match = re.fullmatch(r'(?:(\d+)시간\s*)?(\d+)분\s*(\d+)초', text)
    h, m, s = (int(x or 0) for x in match.groups())
    return None if s >= 60 or h and m >= 60 else (h*3600+m*60+s)*1000


def parse(text, duration_ms):
    matches = list(TIME.finditer(text)); result = []; issues = []; seen = set(); i = 0
    while i < len(matches):
        match = matches[i]; start = milliseconds(match.group()); end = None
        after = match.end(); next_i = i+1
        if next_i < len(matches) and re.fullmatch(r'\s*(?:~|〜|～|-|–|—)\s*', text[after:matches[next_i].start()]):
            end = milliseconds(matches[next_i].group()); after = matches[next_i].end(); next_i += 1
            if end is None or start is None or end <= start or end > duration_ms:
                issues.append({'offset': match.start(), 'reason': 'invalid_range'}); i = next_i; continue
        prefix = text[max(0, match.start()-16):match.start()]
        relative = re.match(r'\s*(후|뒤|전|남음)(?:\s|$)',text[after:])
        ambiguous = relative or re.search(r'(오전|오후|시계|점수|비율|내일|오늘|날짜|뒤|후|전)\s*$', prefix)
        if start is None or not 0 <= start < duration_ms or ambiguous:
            issues.append({'offset': match.start(), 'reason': 'needs_review' if ambiguous else 'invalid_time'})
        elif (start, end) not in seen:
            stop = matches[next_i].start() if next_i < len(matches) else len(text)
            description = text[after:stop].strip(' \n\r/|-~')
            result.append({'start_ms': start, 'end_ms': end, 'description': description,
                           'offset': match.start(), 'parse_kind': 'text_timestamp'})
            seen.add((start,end))
        i = next_i
    return result, issues


class CommentTransport(PublicTransport):
    def get_comments(self, path):
        # Reuse bounded retry / JSON / auth handling without weakening the VOD allowlist.
        self._comment_path = path
        return Transport.get(self, '/service/comments')

    def request(self, ignored):
        from urllib.request import build_opener, HTTPRedirectHandler, Request
        from urllib.error import HTTPError
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise AppError('REDIRECT', '댓글 조회가 예상하지 못한 주소로 이동했습니다.')
        url = BASE + self._comment_path
        try:
            response = build_opener(NoRedirect()).open(Request(url, headers=HEADERS), timeout=20)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(8*1024*1024+1)
            if len(body) > 8*1024*1024:
                raise AppError('RESPONSE_TOO_LARGE', '댓글 응답이 너무 큽니다.')
            return response.status, dict(response.headers), body


class ViewerCommentTransport(CommentTransport):
    def __init__(self, login_ready, **kwargs):
        super().__init__(**kwargs)
        self.viewer = BrowserTransport(login_ready, **kwargs)

    def request(self, ignored):
        return self.viewer.request(BASE+self._comment_path)

    def close(self):
        self.viewer.close()


def fetch(transport, video, max_pages=100, progress=None):
    path = '/type/STREAMING_VIDEO/id/'+video_id(video)+'/comments'
    comments = {}; pages = 0; issues = []; total = None
    def add(items):
        for item in items:
            row = item.get('comment', {})
            if not isinstance(row.get('commentId'), int) or not isinstance(row.get('content'), str):
                raise AppError('COMMENT_SCHEMA', '영상 댓글 형식이 변경되었습니다.')
            if str(row.get('objectId', video)) != video:
                raise AppError('COMMENT_SOURCE', '다른 영상의 댓글이 반환되었습니다.')
            cid = str(row['commentId'])
            comments[cid] = {k: row.get(k) for k in ('content','parentCommentId','createdDate','modifiedDate','deleted','secret','hideByCleanBot','replyCount')}
            comments[cid].update(comment_id=cid, user_id=(item.get('user') or {}).get('userIdHash'))
            add(item.get('replyComments') or [])
    try:
        for page in range(max_pages):
            data = transport.get_comments(path+'?'+urlencode({'limit':30,'offset':page*30,'orderType':'ASC','pagingType':'PAGE'}))
            block = data.get('comments', {})
            if not isinstance(block.get('data'), list) or type(block.get('commentCount')) is not int:
                raise AppError('COMMENT_SCHEMA', '영상 댓글 목록 형식이 변경되었습니다.')
            if data.get('commentActive') is False:
                raise AppError('COMMENTS_DISABLED', '영상 댓글이 비활성화되어 있습니다.')
            total = block['commentCount']; before = len(comments)
            add(data.get('bestComments') or []); add(block['data']); pages += 1
            if progress: progress({'phase':'comments','pages':pages,'comment_count':len(comments)})
            if (page+1)*30 >= total: break
            if not block['data'] or len(comments) == before:
                raise AppError('COMMENT_STALLED','댓글 페이지가 진행되지 않습니다.')
        else:
            issues.append('page_budget')
        # Website reply cursor is the last reply's commentId, not a page number.
        for cid, parent in list(comments.items()):
            if parent.get('parentCommentId'): continue
            expected = parent.get('replyCount') or 0
            children = [c for c in comments.values() if str(c.get('parentCommentId')) == cid]
            cursor = 0
            while len(children) < expected and pages < max_pages:
                data = transport.get_comments(path+'/'+cid+'/replyComments?offset='+str(cursor))
                rows = data.get('comments', {}).get('data')
                if not isinstance(rows, list) or not rows:
                    issues.append('reply_incomplete'); break
                add(rows); pages += 1
                next_cursor = rows[-1]['comment']['commentId']
                if next_cursor == cursor: issues.append('reply_stalled'); break
                cursor = next_cursor
                children = [c for c in comments.values() if str(c.get('parentCommentId')) == cid]
            if len(children) < expected: issues.append('reply_incomplete')
    except AppError as error:
        if not comments: raise
        issues.append(error.code)
    return list(comments.values()), {'state':'partial' if issues else 'finished', 'issues':issues,
                                     'pages':pages, 'reported_count':total, 'completeness':'unverified'}


def capture(store, sid, transport=None, text=None, progress=None):
    session = store.session(sid); video = session['video_id']
    if text is None:
        own = transport is None; transport = transport or CommentTransport()
        try: rows, manifest = fetch(transport, video, progress=progress)
        finally:
            if own: transport.close()
        source = 'website'
    else:
        if not isinstance(text, str) or len(text) > 12000:
            raise AppError('INPUT', '붙여넣기는 12,000자 이하 텍스트여야 합니다.')
        rows = [{'comment_id':records.digest(text)[:24], 'content':text, 'user_id':None}]
        manifest = {'state':'partial','issues':['manual_paste'],'pages':0}; source = 'manual_paste'
    recommendations = []; clean = []
    for row in rows:
        author = row.pop('user_id', None)
        row['author_key'] = hmac.new(bytes.fromhex(session['salt']), str(author).encode(), 'sha256').hexdigest()[:24] if author else None
        hidden = row.get('deleted') or row.get('secret') or row.get('hideByCleanBot')
        if hidden: row['content'] = ''; row['status'] = 'unavailable'; parsed, issues = [], []
        else:
            parsed, issues = parse(row['content'], session['duration_ms'])
            row['status'] = 'parsed' if parsed else 'needs_review' if issues else 'no_timestamp'
        row['issues'] = issues; row['body_sha256']=records.digest(row['content']); clean.append(row)
        for entry in parsed:
            rid = records.digest([video,row['comment_id'],row['content'],entry['start_ms'],entry['end_ms']])[:32]
            recommendations.append({**entry,'id':rid,'comment_id':row['comment_id'],
                                    'source_type':'viewer_comment','review_state':'미검토','source':source})
    body = {'video_id':video,'session_id':sid,'source':source,'parser_version':1,
            'comments':clean,'recommendations':recommendations,'manifest':manifest}
    rid = records.save(store,'comments',sid,body)
    records.export(store,sid,'comments',rid,{'comments.jsonl':''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in clean),
                  'recommendations.json':recommendations,'manifest.json':manifest})
    return rid
