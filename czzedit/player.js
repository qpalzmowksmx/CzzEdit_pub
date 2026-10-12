/* Playback uses the media clock, never an estimated wall-clock timer. */
(function(root){
  function activeIndex(rows, ms){
    let low=0, high=rows.length;
    while(low<high){const mid=(low+high)>>>1;if(rows[mid].time_ms<=ms)low=mid+1;else high=mid;}
    return low-1;
  }
  class VodPlayer{
    constructor(video,Hls,getSource,onTime,onStatus){
      Object.assign(this,{video,Hls,getSource,onTime,onStatus,generation:0,run:null,hls:null,ready:false,pending:0,wantsPlay:false});
      for(const event of ['timeupdate','seeking','seeked','pause','play','ratechange'])
        video.addEventListener(event,()=>{if(this.ready)onTime(video.currentTime*1000,event);});
      video.addEventListener('loadedmetadata',()=>{if(!this.run)return;this.ready=true;this.seek(this.pending,this.wantsPlay);});
      video.addEventListener('error',()=>{if(this.run)this.fail('영상을 재생하지 못했습니다. 재생 연결 새로고침을 눌러 주세요.');});
      video.addEventListener('waiting',()=>{if(this.run)this.onStatus('영상 불러오는 중… 채팅은 실제 영상 시각을 따릅니다.');});
      video.addEventListener('playing',()=>this.onStatus('영상 재생 시간에 맞춰 채팅을 표시합니다.'));
    }
    reset(){
      ++this.generation;this.run=null;this.ready=false;
      if(this.hls){this.hls.destroy();this.hls=null;}
      this.video.pause();this.video.removeAttribute('src');this.video.load();
    }
    fail(message){this.onStatus(message,true);}
    seek(seconds,play=true){
      this.pending=Math.max(0,seconds);this.wantsPlay=play;
      if(!this.ready)return;
      const end=this.video.duration;
      this.video.currentTime=Number.isFinite(end)?Math.min(this.pending,Math.max(0,end-.001)):this.pending;
      this.onTime(this.video.currentTime*1000,'seeked');
      if(play)this.video.play().catch(()=>this.onStatus('재생 버튼을 눌러 영상을 시작해 주세요.'));
    }
    async open(run,seconds,force=false){
      if(this.run===run&&!force){this.seek(seconds);return;}
      this.reset();this.run=run;this.pending=seconds;this.wantsPlay=true;
      const generation=this.generation;
      this.onStatus('영상 연결 중…');
      try{
        const source=await this.getSource(run);
        if(generation!==this.generation)return;
        if(this.video.canPlayType('application/vnd.apple.mpegurl')){
          this.video.src=source.url;this.video.load();
        }else if(this.Hls&&this.Hls.isSupported()){
          const hls=this.hls=new this.Hls({startPosition:this.pending,maxBufferLength:30,backBufferLength:30});
          hls.on(this.Hls.Events.ERROR,(_,data)=>{
            if(generation!==this.generation||!data.fatal)return;
            hls.stopLoad();this.fail('영상 연결이 끊겼습니다. 재생 연결 새로고침으로 다시 연결해 주세요.');
          });
          hls.loadSource(source.url);hls.attachMedia(this.video);
        }else this.fail('이 브라우저는 앱 내부 영상 재생을 지원하지 않습니다. 최신 Chrome 또는 Safari에서 열어 주세요.');
      }catch(error){if(generation===this.generation){this.run=null;this.fail(error.message);}}
    }
  }
  root.CzzPlayback={VodPlayer,activeIndex};
  if(typeof module!=='undefined')module.exports=root.CzzPlayback;
})(typeof window!=='undefined'?window:globalThis);
