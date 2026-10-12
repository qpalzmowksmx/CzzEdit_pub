/* Source labels and opt-in controls; text from comments/AI is never HTML. */
window.CzzV2=(()=>{
  let current=null,preview=null,aiRun=null,version=0,previewVersion=0,aiTimer=null;
  const labels={rule:'자동 탐지',viewer_comment:'시청자 추천',ai:'AI 추천'};
  const styles=node('style');styles.textContent='details{margin:14px 0}summary{cursor:pointer}input[type=text],input[type=password],input[type=number],textarea{background:#101815;color:var(--text);border:1px solid #506b5c;border-radius:7px;padding:9px;max-width:100%}textarea{width:100%;min-height:90px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}.v2-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.source-note{white-space:pre-wrap}';document.head.append(styles);
  function input(id,type,placeholder){const el=node('input');el.id=id;el.type=type;el.placeholder=placeholder;el.setAttribute('aria-label',placeholder);return el;}
  function check(id,text,checked=false){const label=node('label'),el=input(id,'checkbox','');el.checked=checked;label.append(el,document.createTextNode(' '+text));return label;}
  function button(text,fn){const el=node('button',text,'secondary');el.type='button';el.onclick=()=>Promise.resolve().then(fn).catch(showError);return el;}
  function panel(title){const el=node('details'),summary=node('summary',title);el.append(summary);$('collect-form').parentElement.append(el);return el;}
  document.querySelector('.options').append(check('include-comments','영상 댓글의 시청자 추천도 수집',true),check('viewer-adaptive','연결한 시청자 표본으로 보정'));
  const cp=panel('영상 댓글만 수집 / 직접 붙여넣기');cp.append(node('p','위 영상 URL 또는 열어 둔 작업에서 영상 아래 댓글을 별도로 읽습니다. 채팅 TXT에는 섞지 않습니다.','muted'));
  const paste=node('textarea');paste.placeholder='자동 조회가 안 되면 시간표가 포함된 공개 댓글을 여기에 붙여넣으세요.';paste.setAttribute('aria-label','시청자 추천 댓글 원문');cp.append(paste);
  cp.append(button('영상 댓글 조회',()=>start('comments',{url:$('url').value,session_id:$('url').value?undefined:current?.body.session_id,login:$('login').checked})),button('붙여넣은 추천 저장',()=>{if(!paste.value.trim())throw Error('댓글 내용을 입력해 주세요.');return start('comments',{url:$('url').value,session_id:$('url').value?undefined:current?.body.session_id,text:paste.value});}));
  const filters=node('div',undefined,'actions');const select=node('select');select.setAttribute('aria-label','후보 출처 필터');for(const [value,label] of Object.entries({all:'전체',...labels})){const op=node('option',label);op.value=value;select.append(op);}select.onchange=()=>{invalidate();render();};filters.append(select);$('markers').before(filters);
  const ap=panel('선택형 AI 분석 · 외부 API');ap.append(node('p','선택한 텍스트만 OpenAI API로 보냅니다. 키는 앱 실행 중 메모리에만 보관합니다. store=false를 사용하지만 제공자의 별도 보관 정책이 적용될 수 있습니다.','muted'));
  const model=input('ai-model','text','계정에서 사용 가능한 모델 ID'),keyInput=input('ai-key','password','OpenAI API 키');keyInput.autocomplete='off';
  const mode=node('select');mode.setAttribute('aria-label','AI 입력 방식');for(const [v,t] of [['candidates','선택 후보 검토'],['range','지정 채팅 구간 탐색']]){const o=node('option',t);o.value=v;mode.append(o);}
  const from=input('ai-from','number','시작 초'),to=input('ai-to','number','끝 초');from.min=to.min='0';from.value='0';
  const aiStatus=node('p','AI를 실행하지 않았습니다.','muted'),previewText=node('pre'),execute=button('확인한 내용으로 AI 실행',async()=>{
    if(!preview)throw Error('먼저 전송 내용을 미리 확인해 주세요.');
    if(keyInput.value){await api('ai/credentials',{key:keyInput.value});keyInput.value='';}
    const result=await api('ai/runs',{preview_id:preview.preview_id,fingerprint:preview.fingerprint});aiRun=result.id;execute.disabled=true;watchAI();
  });execute.disabled=true;
  function invalidate(){previewVersion++;preview=null;execute.disabled=true;previewText.textContent='입력이 바뀌었습니다. 미리보기를 다시 생성해 주세요.';}
  for(const el of [model,mode,from,to])el.oninput=invalidate;
  ap.append(model,keyInput,button('메모리 키 지우기',async()=>{await api('ai/credentials',{key:''});keyInput.value='';aiStatus.textContent='메모리 키를 지웠습니다.';}),mode,from,to);
  ap.append(button('보낼 내용 미리보기',async()=>{
    if(!current)throw Error('먼저 통합 타임라인을 열어 주세요.');
    const request=++previewVersion,tid=current.id;
    const data={timeline_id:tid,model:model.value};
    if(mode.value==='candidates')data.candidate_ids=[...$('markers').querySelectorAll('input:checked')].map(e=>e.value);
    else Object.assign(data,{start_ms:Math.round(Number(from.value)*1000),end_ms:Math.round(Number(to.value)*1000)});
    const result=await api('ai/preview',data);if(request!==previewVersion||current?.id!==tid)return;
    preview=result;execute.disabled=false;
    aiStatus.textContent=`${result.request_count}회 요청 · 입력 추정 상한 ${result.input_token_upper_estimate} / 출력 상한 ${result.output_token_limit} 토큰 · 재시도 포함 최대 20회 · 제외 구간 ${result.omitted.length}개 · 10분 후 만료`;
    previewText.textContent=JSON.stringify({instructions:result.prompt,ranges:result.ranges,chunks:result.chunks,omitted:result.omitted},null,2);
  }),execute,button('AI 작업 중단',async()=>{if(aiRun)await api('ai/runs/'+aiRun+'/cancel',{});}),aiStatus,previewText);
  async function watchAI(){
    clearTimeout(aiTimer);if(!aiRun)return;
    try{const row=await api('ai/runs/'+aiRun),b=row.body;
      aiStatus.textContent=`AI ${b.state} · ${b.completed_requests}/${b.request_count}회 완료 · 추천 ${b.recommendations.length}개 · 검증 제외 ${b.rejected}개`+(b.error?' · '+b.error:'')+` · 미처리 ${(b.unprocessed_chunks||[]).length}개`;
      if(b.state==='running'){aiTimer=setTimeout(watchAI,2000);return;}
      if(current?.id===b.timeline_id){const snap=current.body;const result=await api('timelines',{session_id:snap.session_id,run_id:snap.run_id,ai_id:row.id});await show(result.id);}
    }catch(e){aiStatus.textContent=e.message;}
  }
  const vp=panel('생방송 시청자 수 기록 · 공식 API');
  vp.append(node('p','채팅 수신과 별도 기능입니다. 개발자 Client 인증이 필요하며 방송자 OAuth는 사용하지 않습니다. 저장한 시청자 표본을 다시보기에 시간 대응시킨 뒤 보정 분석할 수 있습니다.','muted'));
  const live=input('live-channel','text','라이브 URL 또는 채널 ID'),client=input('live-client','password','Client ID'),secret=input('live-secret','password','Client Secret');client.autocomplete=secret.autocomplete='off';
  const monitors=node('select');monitors.setAttribute('aria-label','시청자 표본 기록');const viewerStatus=node('p','아직 기록을 조회하지 않았습니다.','muted');
  async function refresh(){const rows=await api('viewers');const selected=monitors.value;monitors.replaceChildren();for(const r of rows){const op=node('option',r.body.label+' · '+r.body.state+' · '+r.sample_count+'개'+(r.body.error?' · '+r.body.error:''));op.value=r.id;monitors.append(op);}if([...monitors.options].some(o=>o.value===selected))monitors.value=selected;viewerStatus.textContent='기록 '+rows.length+'개';}
  vp.append(live,client,secret,button('시청자 기록 시작',async()=>{const channel=live.value.trim().replace(/^https:\/\/chzzk.naver.com\/live\//,'').split(/[?#/]/)[0];await api('viewers/start',{channel_id:channel,client_id:client.value,client_secret:secret.value});client.value=secret.value='';await refresh();}),button('기록 상태 새로고침',refresh),monitors,button('선택 기록 중단',async()=>{await api('viewers/stop',{id:monitors.value});await refresh();}),viewerStatus);
  const mappingText=node('textarea');mappingText.placeholder='[{"video_start_ms":0,"video_end_ms":3600000,"broadcast_start_ms":0}]';mappingText.setAttribute('aria-label','방송과 다시보기 시간 대응 구간');
  vp.append(node('p','시간 대응(밀리초): 잘린 구간이 있다면 여러 구간을 입력하세요. 같은 채널이라는 이유로 자동 연결하지 않습니다.','muted'),mappingText,check('mapping-confirmed','이 기록과 영상이 같은 방송이며 시간 대응을 확인했습니다.'),button('열린 영상에 표본 연결',async()=>{if(!current)throw Error('영상을 먼저 열어 주세요.');await api('viewers/map',{session_id:current.body.session_id,monitor_id:monitors.value,segments:JSON.parse(mappingText.value),confirmed:$('mapping-confirmed').checked});viewerStatus.textContent='시간 대응을 저장했습니다. 시청자 보정 옵션을 켜고 다시 분석해 주세요.';}));
  async function show(id){
    const request=++version;++runRequest;const row=await api('timelines/'+id);if(request!==version)return;
    current=row;selectedMarker=null;chatVersion++;syncRequest++;player.reset();syncRows=[];syncBucket=null;$('chat-panel').hidden=true;$('chart').hidden=true;
    invalidate();to.value=String(Math.floor(row.body.duration_ms/1000));
    $('result-title').textContent='통합 편집 후보 '+row.body.items.length+'개';$('result-note').textContent=row.body.title+' · 영상 시각 순 · 출처별 원본 보존 · 수집 완전성 미확인';
    const prefix=row.body.session_id+'/timelines/'+row.id+'/';$('downloads').replaceChildren(...['txt','csv','json'].map(ext=>link('통합 '+ext.toUpperCase(),prefix+'timeline.'+ext)));
    if(row.body.max_seq)$('downloads').append(link('채팅 TXT',row.body.session_id+'/chat.txt'));
    if(row.body.run_id)$('downloads').append(link('규칙 분석 TXT',row.body.session_id+'/'+row.body.run_id+'/timeline.txt'));
    if(row.body.comment_id)$('downloads').append(link('댓글 수집 상태',row.body.session_id+'/comments/'+row.body.comment_id+'/manifest.json'));
    if(row.body.ai_id)$('downloads').append(link('AI 결과 TXT',row.body.session_id+'/ai/'+row.body.ai_id+'/recommendations.txt'));
    render();
    if(row.body.run_id){
      const run=await api('runs/'+row.body.run_id);if(request!==version)return;
      const canvas=$('chart');canvas.hidden=false;const ctx=canvas.getContext('2d');ctx.clearRect(0,0,800,130);
      const windows=run.windows,max=windows.reduce((n,w)=>Math.max(n,w.n),1);ctx.strokeStyle='#95e6af';ctx.lineWidth=2;ctx.beginPath();
      windows.forEach((w,i)=>{const x=i/Math.max(1,windows.length-1)*800,y=120-w.n/max*110;if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();
      if(run.config.viewer_adaptive){const count=windows.filter(w=>w.viewer_baseline?.mode==='viewer_adaptive').length;$('result-note').textContent+=' · '+(count?'시청자 보정 적용 구간 있음':'시청자 표본 미확보·학습 대기 · 기존 채팅 기준 적용');}
    }
  }
  function render(){
    if(!current)return;$('markers').replaceChildren();
    const items=current.body.items.filter(i=>select.value==='all'||select.value===i.source_type);
    if(!items.length)$('markers').append(node('p','이 출처의 후보가 없습니다.','empty'));
    for(const m of items){const card=node('article',undefined,'marker');card.dataset.marker=m.id;
      const title=button(stamp(m.anchor_ms)+' · '+labels[m.source_type]+' · '+m.title,()=>selectMarker('timeline:'+current.id,m));title.className='marker-title';
      card.append(title);
      if(m.source_type!=='ai'){const label=check('candidate-'+m.id,'AI 검토에 포함');label.firstChild.value=m.id;label.firstChild.onchange=invalidate;card.append(label);}
      if(m.comment_text)card.append(node('p','영상 댓글 원문\n'+m.comment_text,'source-note'));
      if(m.source_type==='rule'){card.append(node('p',m.baseline_ready?'기준선 확보':'기준 학습 중인 임시 후보','muted'),node('p','정점 '+stamp(m.peak_ms)+' · 유효 채팅 '+m.peak_stats.n+'건 · 작성자 '+m.peak_stats.u+'명','muted'));card.append(link('근거 채팅 TXT',current.body.session_id+'/'+current.body.run_id+'/markers/'+m.origin_id+'.txt'));if(m.viewer_baseline)card.append(node('p',m.viewer_baseline.mode==='viewer_adaptive'?'시청자 보정 적용 · 기준 '+Math.round(m.viewer_baseline.reference)+'명':'시청자 자료 미확보 · 기존 채팅 기준','muted'));}
      if(m.source_type==='ai')card.append(node('p',m.rationale),node('p','판단 한계: '+m.uncertainty,'muted'));
      $('markers').append(card);
    }
  }
  return {show,reset:()=>{version++;previewVersion++;current=null;preview=null;aiRun=null;clearTimeout(aiTimer);},forRun:async(id,request)=>{const result=await api('timelines',{run_id:id});if(request===runRequest)await show(result.id);}};
})();
