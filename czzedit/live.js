/* LIVE has its own selection, cancellation, errors and incremental cursors. */
(() => {
  let sid=null, seq=null, revision=0, follow=true, generation=0, selected=null, fetching=false, focus=false;
  let pendingRows=[], unseen=0;
  let sessionSignature='', candidates=new Map(), lastExport=null, requestVersion=0;
  const states={starting:'연결 준비 중',collecting:'채팅 수신 중',reconnecting:'재연결 중',stopping:'남은 자료 저장 중',stopped:'중단됨',ended:'방송 종료',error:'확인 필요',incomplete:'저장 상태 확인 필요'};
  const terminal=new Set(['stopped','ended','error','incomplete']);
  const fail=e=>{$('live-error').textContent=e.message;};
  const endpoint=tail=>'live/sessions/'+sid+(tail?'/'+tail:'');
  window.CzzLive={focused:()=>focus,reset:()=>{pick(null);sessionSignature='';}};
  $('live-card').addEventListener('focusin',()=>{focus=true;});
  $('collect-form').addEventListener('focusin',()=>{focus=false;});
  $('sessions').addEventListener('click',()=>{focus=false;});
  function chatRow(e,evidence=new Set()){
    const row=node('div',undefined,'chat-row'+(evidence.has(e.event_id)?' evidence':''));
    row.append(node('span',stamp(e.time_ms)+' '),node('span',e.author_key?.slice(0,8)||'작성자 미상','chat-author'),node('div',e.content));
    if(e.late)row.append(node('small','지연·접속 전 채팅: 확정 후보에는 미반영'));
    return row;
  }
  function pick(id){sid=id||null;seq=null;revision=0;selected=null;lastExport=null;candidates.clear();requestVersion++;
    $('live-chats').replaceChildren();$('live-markers').replaceChildren();$('live-downloads').replaceChildren();$('live-context').hidden=true;
    $('live-workspace').hidden=!sid;follow=true;focus=true;pendingRows=[];unseen=0;
    if(!sid){$('live-status').textContent='공개 라이브 URL을 입력하거나 숨긴 작업을 다시 표시하세요.';$('live-note').textContent='';$('live-error').textContent='';}
  }
  $('live-sessions').onchange=()=>{pick($('live-sessions').value);refresh().catch(fail);};
  $('live-inspect').onclick=async()=>{
    $('live-inspect').disabled=true;
    try{const m=await api('live/inspect',{url:$('live-url').value});$('live-error').textContent='';$('live-status').textContent=m.title+' · '+(m.status==='OPEN'?'방송 중 · 수집 시작으로 실제 채팅 연결':'현재 생방송이 아닙니다');}
    catch(e){fail(e);}finally{$('live-inspect').disabled=false;}
  };
  $('live-form').onsubmit=async e=>{
    e.preventDefault();$('live-start').disabled=true;
    try{
      if($('live-url').value.includes('/video/'))throw Error('다시보기 URL은 위 다시보기 입력칸을 사용해 주세요.');
      const r=await api('live/start',{url:$('live-url').value,sensitive:$('live-sensitive').checked,viewer_adaptive:$('live-viewers').checked});
      pick(r.id);$('live-error').textContent='';await refresh();
    }catch(error){fail(error);}finally{$('live-start').disabled=false;}
  };
  $('live-stop').onclick=async()=>{try{await api(endpoint('stop'),{});$('live-status').textContent='남은 자료를 저장하고 있습니다…';}catch(e){fail(e);}};
  $('live-resume').onclick=async()=>{try{await api(endpoint('resume'),{});$('live-error').textContent='';await refresh();}catch(e){fail(e);}};
  $('live-export').onclick=async()=>{try{await api(endpoint('export'),{});$('live-export').disabled=true;}catch(e){fail(e);}};
  function jumpLatest(){follow=true;for(const e of pendingRows)$('live-chats').append(chatRow(e));pendingRows=[];unseen=0;while($('live-chats').children.length>300)$('live-chats').firstChild.remove();$('live-chats').scrollTop=$('live-chats').scrollHeight;$('live-follow-note').textContent='최신 채팅을 따라갑니다.';}
  $('live-follow').onclick=jumpLatest;
  $('live-chats').addEventListener('scroll',()=>{const el=$('live-chats');if(el.scrollHeight-el.scrollTop-el.clientHeight>60){follow=false;$('live-follow-note').textContent='과거 채팅을 읽는 중입니다. 최신으로 이동하려면 위 버튼을 누르세요.';}});
  async function context(marker){
    selected=marker.id;const own=sid,version=++requestVersion;
    $('live-context').hidden=false;$('live-context-title').textContent=stamp(marker.peak_ms)+' 부근 채팅';
    try{const r=await api(endpoint('chats')+'?time_ms='+marker.peak_ms);if(own!==sid||version!==requestVersion)return;
      const evidence=new Set(marker.evidence_ids);$('live-context-chats').replaceChildren(...r.chats.map(e=>chatRow(e,evidence)));
      if(!r.chats.length)$('live-context-chats').append(node('p','이 시간대에 저장된 채팅이 없습니다.','muted'));
      for(const el of $('live-markers').children)el.classList.toggle('selected',el.dataset.id===selected);
    }catch(e){if(own===sid)fail(e);}
  }
  function updateMarker(m){
    candidates.set(m.id,m);let card=[...$('live-markers').children].find(e=>e.dataset.id===m.id);
    if(!card){card=node('article',undefined,'marker');card.dataset.id=m.id;
      const before=[...$('live-markers').children].find(el=>candidates.get(el.dataset.id).clip_start_ms>m.clip_start_ms);
      $('live-markers').insertBefore(card,before||null);
    }
    const button=node('button',stamp(m.clip_start_ms)+' — '+stamp(m.clip_end_ms),'marker-title');button.onclick=()=>context(m);
    card.replaceChildren(button,node('p',m.reasons.join(' · ')),node('small',({generating:'생성 중',waiting_context:'앞뒤 채팅 기다리는 중',confirmed:'확정',partial:'부분 문맥으로 마감'})[m.state]||m.state));
    if(m.baseline_ready===false)card.append(node('small',' · 기준 학습 중'));
    card.classList.toggle('selected',m.id===selected);
  }
  function downloads(body){
    const ex=body.export||{};const id=ex.state==='completed'?ex.id:body.checkpoint_id;
    $('live-export').disabled=ex.state==='exporting';
    if(ex.state==='error')fail(new Error(ex.error));
    const signature=body.id+':'+id;if(signature===lastExport)return;lastExport=signature;
    $('live-downloads').replaceChildren(link('수신 중 채팅 TXT','live/'+body.id+'/chat.received.txt'));
    if(id){const prefix='live/'+body.id+'/checkpoints/'+id+'/';
      $('live-downloads').append(link('시간순 채팅 TXT',prefix+'chat.txt'),link('편집점 TXT',prefix+'timeline.txt'),link('편집점 CSV',prefix+'timeline.csv'),link('수집 상태',prefix+'analysis-manifest.json'));
    }
  }
  async function refresh(){
    if(fetching)return;fetching=true;
    try{
      const list=await api('live/sessions');
      if(sid&&!list.some(s=>s.id===sid))pick(null);
      const signature=JSON.stringify(list.map(s=>[s.id,s.state,s.title]));
      if(signature!==sessionSignature){sessionSignature=signature;const options=[node('option','기록 선택')];options[0].value='';
        for(const s of list){const o=node('option',s.title+' · '+(states[s.state]||s.state));o.value=s.id;options.push(o);}
        $('live-sessions').replaceChildren(...options);$('live-sessions').value=sid||'';
      }
      if(!sid&&list.length){pick(list[0].id);focus=false;$('live-sessions').value=sid;}
      if(!sid)return;
      const own=sid;
      const b=await api(endpoint(''));if(own!==sid)return;
      $('live-status').textContent=b.title+' · '+(states[b.state]||b.state)+' · 채팅 '+b.event_count.toLocaleString()+'건';
      $('live-note').textContent=(b.time_basis==='broadcast_elapsed'?'방송 시작 후 ':'수집 시작 후 ')+stamp(b.watermark_ms)+' · '+(b.generation>1?'연결 복구 '+(b.generation-1)+'회 · ':'')+'시청자 '+(b.viewer_count??'자료 없음')+' · 보정 '+({active:'적용 중',warming_up:'학습 중',missing:'자료 부족'})[b.viewer_status]+(b.gaps.length?' · 미관측/끊김 구간 '+b.gaps.length+'개':'');
      $('live-stop').hidden=terminal.has(b.state);$('live-resume').hidden=!terminal.has(b.state);
      $('live-candidate-count').textContent=b.marker_count;
      if(b.error||b.output_error)$('live-error').textContent=[b.error,b.output_error].filter(Boolean).join('\n');
      else if(b.state==='collecting'&&generation!==b.generation)$('live-error').textContent='';
      generation=b.generation;downloads(b);
      const r=await api(endpoint('events')+(seq===null?'?limit=300':'?limit=500&after_seq='+seq));if(own!==sid)return;
      const container=$('live-chats');
      if(follow){for(const e of r.events)container.append(chatRow(e));jumpLatest();}
      else if(r.events.length){pendingRows.push(...r.events);pendingRows=pendingRows.slice(-300);unseen+=r.events.length;$('live-follow-note').textContent='과거 채팅을 읽는 중 · 새 채팅 '+unseen+'건';}
      seq=r.next_seq;
      const marks=await api(endpoint('candidates')+'?after_revision='+revision);if(own!==sid)return;
      for(const m of marks.markers)updateMarker(m);revision=marks.revision;
    }finally{fetching=false;}
  }
  async function tick(){try{await refresh();}catch(e){fail(e);}finally{setTimeout(tick,document.hidden?5000:1000);}}
  tick();
})();
