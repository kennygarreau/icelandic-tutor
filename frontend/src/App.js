import React, { useState, useRef, useEffect, useCallback } from 'react';
import './App.css';
import { tracer, SpanStatusCode } from './telemetry';

const API     = '/api';
const WHISPER = '/whisper';
const TTS     = '/tts';
const PRONUN  = '/pronunciation';
const LEVELS  = ['beginner','intermediate','advanced'];

const WELCOME_MSG = {
  id:0, role:'assistant',
  icelandic:'Halló! Ég heiti Sigríður og ég er kennarinn þinn í íslensku. Hvernig hefur þú það í dag?',
  correction:{errors:[],positive:"Welcome! I'm ready to help you learn Icelandic.",
    tip:'Try: "Mér líður vel, takk!" (I\'m doing well, thanks!)'},
};

function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v));}

let _launchChat = null;
function launchChat(mode,id){_launchChat?.(mode,id);}

const playWord=async(text)=>{
  try{
    const r=await fetch(`${TTS}/synthesize`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,speed:0.8})});
    if(!r.ok)return;
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const audio=new Audio(url);
    audio.onended=()=>URL.revokeObjectURL(url);
    await audio.play();
  }catch(e){console.error(e);}
};

// ═══════════════════════════════════════════════════════════════════════════════
// ROOT
// ═══════════════════════════════════════════════════════════════════════════════
export default function App(){
  const [tab,setTab]=useState('chat');
  const TABS=[
    {id:'chat',      icon:<ChatIcon/>,  label:'Chat'},
    {id:'scenarios', icon:<SceneIcon/>, label:'Scenarios'},
    {id:'lessons',   icon:<BookIcon/>,  label:'Lessons'},
    {id:'heatmap',   icon:<FireIcon/>,  label:'Heatmap'},
    {id:'progress',  icon:<ChartIcon/>, label:'Progress'},
    {id:'flashcards',icon:<CardIcon/>,  label:'Cards'},
    {id:'cefr',      icon:<CefrIcon/>,  label:'CEFR'},
  ];
  const goChat=(mode,id)=>{setTab('chat'); setTimeout(()=>launchChat(mode,id),50);};

  useEffect(()=>{
    const nav=document.getElementById('bottom-nav');
    const check=()=>{if(nav) nav.style.display=window.innerWidth<=640?'flex':'none';};
    check();
    window.addEventListener('resize',check);
    return()=>window.removeEventListener('resize',check);
  },[]);

  return(
    <div className="app">
      <div className="aurora" aria-hidden="true">
        <div className="aurora-band a1"/><div className="aurora-band a2"/><div className="aurora-band a3"/>
      </div>
      <nav className="sidebar">
        <div className="sidebar-brand">
          <span className="rune">ᛁ</span>
          <div><div className="brand-name">Sigríður</div><div className="brand-sub">Íslenska</div></div>
        </div>
        {TABS.map(t=>(
          <button key={t.id} className={`nav-btn ${tab===t.id?'active':''}`} onClick={()=>setTab(t.id)}>
            {t.icon}<span>{t.label}</span>
          </button>
        ))}
      </nav>
      <main className="main">
        {tab==='chat'       && <ChatView/>}
        {tab==='scenarios'  && <ScenariosView onStart={(id)=>goChat('scenario',id)}/>}
        {tab==='lessons'    && <LessonsView   onStart={(id)=>goChat('lesson',id)}/>}
        {tab==='heatmap'    && <HeatmapView/>}
        {tab==='progress'   && <ProgressView/>}
        {tab==='flashcards' && <FlashcardsView/>}
        {tab==='cefr'       && <CefrView/>}
      </main>
      <nav className="bottom-nav" id="bottom-nav" style={{display:'none'}}>
        {TABS.map(t=>(
          <button key={t.id} className={`bottom-nav-btn ${tab===t.id?'active':''}`} onClick={()=>setTab(t.id)}>
            {t.icon}<span>{t.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function ChatView(){
  const [messages,  setMessages]  = useState([WELCOME_MSG]);
  const [sessionId, setSessionId] = useState(null);
  const [level,     setLevel]     = useState('beginner');
  const [loading,   setLoading]   = useState(false);
  const [playingId, setPlayingId] = useState(null);
  const [correction,setCorrection]= useState(WELCOME_MSG.correction);
  const [newVocab,  setNewVocab]  = useState([]);
  const [autoPlay,  setAutoPlay]  = useState(true);
  const [speed,     setSpeed]     = useState(0.85);
  const [chatMode,  setChatMode]  = useState({mode:'free',id:null,label:''});
  const [pronScore, setPronScore] = useState(null);
  const [shownTranslations, setShownTranslations] = useState({});

  const chatEndRef     = useRef(null);
  const inputRef       = useRef(null);
  const currentAudioRef= useRef(null);
  const stateRef       = useRef({});

  useEffect(()=>{
    _launchChat=(mode,id)=>{
      setChatMode({mode,id,label:''});
      setMessages([WELCOME_MSG]); setSessionId(null); setPronScore(null); setNewVocab([]);
      if(mode==='scenario') fetch(`${API}/scenarios/${id}`).then(r=>r.json()).then(s=>setChatMode(c=>({...c,label:`🎭 ${s.title}`})));
      if(mode==='lesson')   fetch(`${API}/lessons/${id}`).then(r=>r.json()).then(l=>setChatMode(c=>({...c,label:`📖 ${l.title}`})));
    };
    return()=>{_launchChat=null;};
  },[]);

  useEffect(()=>{chatEndRef.current?.scrollIntoView({behavior:'smooth'});},[messages]);

  const speakText=useCallback(async(text,msgId)=>{
    setPlayingId(msgId);
    try{
      const r=await fetch(`${TTS}/synthesize`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,speed})});
      if(!r.ok)throw new Error();
      const blob=await r.blob();const url=URL.createObjectURL(blob);
      const audio=new Audio(url);
      currentAudioRef.current=audio;
      audio.onended=()=>{setPlayingId(null);URL.revokeObjectURL(url);currentAudioRef.current=null;};
      audio.onerror=()=>{setPlayingId(null);URL.revokeObjectURL(url);currentAudioRef.current=null;};
      await audio.play();
    }catch{setPlayingId(null);}
  },[speed]);

  // Always-current snapshot — sendMessage reads from here so it needs zero deps
  stateRef.current={messages,level,autoPlay,sessionId,chatMode,speakText};

  const sendMessage=useCallback(async(text,audioBlob=null)=>{
    const{messages,level,autoPlay,sessionId,chatMode,speakText}=stateRef.current;
    const userText=text.trim();if(!userText)return;
    const userMsg={id:Date.now(),role:'user',text:userText};
    const nextMessages=[...messages,userMsg];
    setMessages(nextMessages);setLoading(true);setPronScore(null);

    const history=nextMessages.filter(m=>m.role==='user'||m.role==='assistant')
      .map(m=>({role:m.role,content:m.role==='user'?m.text:m.icelandic}));

    const prevIcelandic=[...messages].reverse().find(m=>m.role==='assistant')?.icelandic;
    if(audioBlob&&prevIcelandic) scorePronunciation(audioBlob,prevIcelandic,userText);

    const streamId=Date.now()+1;
    const _span=tracer.startSpan('chat.turn',{attributes:{'chat.level':level,'chat.mode':chatMode.mode,'input.length':userText.length}});
    const _t0=Date.now();

    try{
      const resp=await fetch(`${API}/chat/stream`,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:sessionId,messages:history,level,
          mode:chatMode.mode,
          scenario_id:chatMode.mode==='scenario'?chatMode.id:null,
          lesson_id:chatMode.mode==='lesson'?chatMode.id:null})});
      if(!resp.ok) throw new Error();

      const reader=resp.body.getReader();
      const decoder=new TextDecoder();
      let buf='';
      let started=false;

      while(true){
        const{done,value}=await reader.read();
        if(done) break;
        buf+=decoder.decode(value,{stream:true});

        const blocks=buf.split('\n\n');
        buf=blocks.pop();

        for(const block of blocks){
          if(!block.startsWith('data: ')) continue;
          let evt;
          try{evt=JSON.parse(block.slice(6));}catch{continue;}

          if(evt.t==='tok'){
            if(!started){
              _span.addEvent('first_token',{'ttft_ms':Date.now()-_t0});
              setMessages(prev=>[...prev,{id:streamId,role:'assistant',icelandic:evt.v,streaming:true}]);
              started=true;
            } else {
              setMessages(prev=>prev.map(m=>m.id===streamId?{...m,icelandic:m.icelandic+evt.v}:m));
            }
          } else if(evt.t==='done'){
            _span.addEvent('stream_done',{'total_ms':Date.now()-_t0});
            _span.setStatus({code:SpanStatusCode.OK});
            if(!sessionId) setSessionId(evt.session_id);
            setCorrection(evt.english_correction);
            setNewVocab(evt.new_vocabulary||[]);
            setMessages(prev=>prev.map(m=>m.id===streamId
              ?{...m,icelandic:evt.icelandic,english_translation:evt.english_translation,
                correction:evt.english_correction,lesson_progress:evt.lesson_progress,streaming:false}
              :m));
            if(autoPlay) speakText(evt.icelandic,streamId);
          } else if(evt.t==='error'){
            setMessages(prev=>[...prev,{id:Date.now(),role:'error',text:evt.msg}]);
          }
        }
      }
      setMessages(prev=>prev.map(m=>m.id===streamId&&m.streaming?{...m,streaming:false}:m));
    }catch{
      _span.setStatus({code:SpanStatusCode.ERROR});
      setMessages(prev=>[...prev,{id:Date.now()+1,role:'error',text:'Connection error — is the backend running?'}]);
    }finally{_span.end();setLoading(false);inputRef.current?.focus();}
  },[]);

  const scorePronunciation=async(blob,expectedText,spokenText)=>{
    const _span=tracer.startSpan('pronunciation.score',{attributes:{'expected.length':expectedText.length}});
    try{
      const form=new FormData();
      form.append('audio',blob,'rec.webm');
      form.append('expected_text',expectedText);
      form.append('spoken_text',spokenText);
      const r=await fetch(`${PRONUN}/score`,{method:'POST',body:form});
      if(!r.ok){_span.setStatus({code:SpanStatusCode.ERROR});return;}
      const result=await r.json();
      _span.setAttributes({'score.overall':result.overall_score??0,'score.grade':result.grade??''});
      _span.setStatus({code:SpanStatusCode.OK});
      setPronScore(result);
    }catch(e){_span.setStatus({code:SpanStatusCode.ERROR});console.error('Pron:',e);}
    finally{_span.end();}
  };

  const toggleTranslation=id=>setShownTranslations(prev=>({...prev,[id]:!prev[id]}));
  const newSession=()=>{
    setMessages([WELCOME_MSG]);setSessionId(null);setCorrection(WELCOME_MSG.correction);
    setNewVocab([]);setPronScore(null);setChatMode({mode:'free',id:null,label:''});
  };

  const isStreaming=messages.some(m=>m.streaming);

  return(
    <div className="chat-layout">
      <div className="chat-col">
        <div className="chat-topbar">
          <div className="chat-topbar-left">
            <span className="topbar-title">Conversation</span>
            {chatMode.label&&<span className="mode-badge">{chatMode.label}</span>}
            {sessionId&&!chatMode.label&&<span className="session-badge">Active</span>}
          </div>
          <div className="chat-topbar-right">
            <div className="level-pills">
              {LEVELS.map(l=>(
                <button key={l} className={`pill ${level===l?'active':''}`} onClick={()=>setLevel(l)}>
                  {l.charAt(0).toUpperCase()+l.slice(1)}
                </button>
              ))}
            </div>
            <button className="icon-btn" onClick={newSession} title="New session"><PlusIcon/></button>
          </div>
        </div>

        <WordOfDayCard/>
        <div className="messages">
          {messages.map(msg=>(
            <div key={msg.id} className={`msg msg-${msg.role}`}>
              {msg.role==='assistant'&&(
                <>
                  <div className="msg-avatar">S</div>
                  <div className="msg-body">
                    <p className="msg-text icelandic">{msg.icelandic}{msg.streaming&&<span className="stream-cursor">▋</span>}</p>
                    {shownTranslations[msg.id]&&msg.english_translation&&(
                      <p className="msg-translation">{msg.english_translation}</p>
                    )}
                    {msg.lesson_progress&&<LessonProgressBar progress={msg.lesson_progress}/>}
                    <div className="msg-actions">
                      <button className={`speak-btn ${playingId===msg.id?'playing':''}`}
                        onClick={()=>speakText(msg.icelandic,msg.id)}>
                        {playingId===msg.id?<WaveIcon/>:<SpeakerIcon/>}
                      </button>
                      {msg.english_translation&&(
                        <button className={`translate-btn ${shownTranslations[msg.id]?'active':''}`}
                          onClick={()=>toggleTranslation(msg.id)} title="Show English translation">
                          🌐
                        </button>
                      )}
                    </div>
                  </div>
                </>
              )}
              {msg.role==='user'&&(
                <div className="msg-body user-body"><p className="msg-text">{msg.text}</p></div>
              )}
              {msg.role==='error'&&<div className="msg-error">{msg.text}</div>}
            </div>
          ))}
          {loading&&!isStreaming&&(
            <div className="msg msg-assistant">
              <div className="msg-avatar">S</div>
              <div className="msg-body"><div className="typing-dots"><span/><span/><span/></div></div>
            </div>
          )}
          <div ref={chatEndRef}/>
        </div>

        <ChatInput
          loading={loading}
          onSend={sendMessage}
          autoPlay={autoPlay}
          onAutoPlayChange={setAutoPlay}
          speed={speed}
          onSpeedChange={setSpeed}
          inputRef={inputRef}
          currentAudioRef={currentAudioRef}
          onStopAudio={()=>setPlayingId(null)}
          onClearScore={()=>setPronScore(null)}
        />
      </div>

      <div className="feedback-col">
        <div className="feedback-header"><h2>Feedback</h2></div>
        {pronScore&&<PronunciationPanel score={pronScore}/>}
        {correction&&(
          <div className="correction-body">
            {correction.positive&&(
              <div className="correction-block positive">
                <span className="block-icon">✦</span>
                <div><p className="block-label">Well done</p><p>{correction.positive}</p></div>
              </div>
            )}
            {correction.errors?.length>0&&(
              <div className="correction-block errors">
                <span className="block-icon">⟳</span>
                <div className="errors-list">
                  <p className="block-label">Corrections</p>
                  {correction.errors.map((err,i)=>(
                    <div key={i} className="error-item">
                      <div className="error-line">
                        <span className="wrong">{err.original}</span>
                        <span className="arrow">→</span>
                        <span className="right">{err.correction}</span>
                      </div>
                      <p className="error-explain">{err.explanation}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {correction.tip&&(
              <div className="correction-block tip">
                <span className="block-icon">◈</span>
                <div><p className="block-label">Tip</p><p>{correction.tip}</p></div>
              </div>
            )}
          </div>
        )}
        {newVocab.length>0&&(
          <div className="vocab-block">
            <p className="block-label vocab-label">✦ New vocabulary saved</p>
            {newVocab.map((v,i)=>(
              <div key={i} className="vocab-item">
                <span className="vocab-is">{v.icelandic}</span>
                <span className="vocab-en">{v.english}</span>
                {v.notes&&<p className="vocab-note">{v.notes}</p>}
              </div>
            ))}
          </div>
        )}
        <div className="phoneme-footer">
          <p className="footer-label">Pronunciation</p>
          <div className="phoneme-grid">
            {[['þ','th in "think"'],['ð','th in "this"'],['æ','eye'],['ö','u in "burn"'],
              ['á','ow in "cow"'],['í/ý','ee'],['ú','oo'],['é','ye']].map(([ch,hint])=>(
              <div key={ch} className="phoneme">
                <span className="ph-char">{ch}</span><span className="ph-hint">{hint}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT INPUT — isolated so keystrokes never re-render the message list
// ═══════════════════════════════════════════════════════════════════════════════
const ChatInput=React.memo(function ChatInput({loading,onSend,autoPlay,onAutoPlayChange,speed,onSpeedChange,inputRef,currentAudioRef,onStopAudio,onClearScore}){
  const [input,setInput]=useState('');
  const [recording,setRecording]=useState(false);
  const mediaRecorder=useRef(null);
  const audioChunks=useRef([]);
  const recordingStartRef=useRef(null);
  const isProcessingRef=useRef(false);

  const startRecording=async()=>{
    if(currentAudioRef.current){
      currentAudioRef.current.pause();
      currentAudioRef.current=null;
      onStopAudio();
    }
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:true});
      audioChunks.current=[];
      mediaRecorder.current=new MediaRecorder(stream,{mimeType:'audio/webm'});
      mediaRecorder.current.ondataavailable=e=>{if(e.data.size>0)audioChunks.current.push(e.data);};
      mediaRecorder.current.start();
      recordingStartRef.current=Date.now();
      setRecording(true);onClearScore();
    }catch{alert('Microphone access denied.');}
  };

  const handleAudioStop=async()=>{
    const duration=Date.now()-(recordingStartRef.current||0);
    const blob=new Blob(audioChunks.current,{type:'audio/webm'});
    if(blob.size===0||duration<500)return;
    const form=new FormData();form.append('audio',blob,'rec.webm');form.append('language','is');
    const _span=tracer.startSpan('voice.turn',{attributes:{'recording.duration_ms':duration,'audio.bytes':blob.size}});
    try{
      const r=await fetch(`${WHISPER}/transcribe`,{method:'POST',body:form});
      if(!r.ok){_span.setStatus({code:SpanStatusCode.ERROR});return;}
      const d=await r.json();
      _span.addEvent('transcribed',{'text':d.text||'','language':d.language||''});
      if(d.text?.trim()) await onSend(d.text.trim(),blob);
    }catch(e){_span.setStatus({code:SpanStatusCode.ERROR});console.error(e);}
    finally{_span.end();}
  };

  const stopRecording=()=>{
    if(mediaRecorder.current&&mediaRecorder.current.state!=='inactive'){
      mediaRecorder.current.addEventListener('dataavailable',e=>{
        if(e.data.size>0)audioChunks.current.push(e.data);
        mediaRecorder.current.stream.getTracks().forEach(t=>t.stop());
        setRecording(false);
        if(!isProcessingRef.current){
          isProcessingRef.current=true;
          handleAudioStop().finally(()=>{isProcessingRef.current=false;});
        }
      },{once:true});
      mediaRecorder.current.stop();
    }else{setRecording(false);}
  };

  const handleSend=()=>{
    const t=input.trim();if(!t||loading)return;
    setInput('');
    onSend(t,null);
  };

  return(
    <div className="input-area">
      <div className="input-row">
        <textarea ref={inputRef} className="chat-input"
          placeholder="Skrifaðu á íslensku… (Write in Icelandic…)"
          value={input} onChange={e=>setInput(e.target.value)}
          onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();handleSend();}}}
          rows={2} disabled={loading||recording}/>
        <button className={`mic-btn ${recording?'recording':''}`}
          onMouseDown={e=>{e.preventDefault();if(!recording)startRecording();}}
          onMouseUp={e=>{e.preventDefault();if(recording)stopRecording();}}
          onTouchStart={e=>{e.preventDefault();if(!recording)startRecording();}}
          onTouchEnd={e=>{e.preventDefault();if(recording)stopRecording();}}
          disabled={loading}>
          {recording?<MicActiveIcon/>:<MicIcon/>}
        </button>
        <button className="send-btn" onClick={handleSend}
          disabled={loading||!input.trim()}><SendIcon/></button>
      </div>
      <div className="input-meta">
        <label className="toggle">
          <input type="checkbox" checked={autoPlay} onChange={e=>onAutoPlayChange(e.target.checked)}/>Auto-play
        </label>
        <label className="speed-ctrl">
          Speed
          <input type="range" min="0.5" max="1.5" step="0.05" value={speed}
            onChange={e=>onSpeedChange(parseFloat(e.target.value))}/>
          <span>{speed.toFixed(2)}×</span>
        </label>
      </div>
    </div>
  );
});

function PronunciationPanel({score}){
  const pct=Math.round(score.overall_score);
  const col=pct>=80?'var(--aurora-g)':pct>=60?'var(--gold)':'var(--red)';
  return(
    <div className="pron-panel">
      <div className="pron-header">
        <span className="block-label">🎙 Pronunciation</span>
        <div className="pron-score-circle" style={{color:col,borderColor:col}}>
          <span className="pron-pct">{pct}</span>
          <span className="pron-pct-label">%</span>
        </div>
      </div>
      {score.word_scores?.length>0&&(
        <div className="pron-words">
          {score.word_scores.map((w,i)=>{
            const wp=Math.round(w.score||0);
            const wc=wp>=80?'good':wp>=55?'ok':'bad';
            return <div key={i} className={`pron-word pron-${wc}`} title={`${wp}%`}>{w.word}</div>;
          })}
        </div>
      )}
      {score.phoneme_issues?.length>0&&(
        <div className="pron-issues">
          {score.phoneme_issues.slice(0,2).map((p,i)=>(
            <p key={i} className="pron-issue">• {p.tip}</p>
          ))}
        </div>
      )}
    </div>
  );
}

function LessonProgressBar({progress}){
  if(!progress?.goal_percent) return null;
  const pct=clamp(progress.goal_percent,0,100);
  return(
    <div className="lesson-progress-bar">
      <div className="lpb-fill" style={{width:`${pct}%`}}/>
      <span className="lpb-label">Lesson goal: {pct}%</span>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// SCENARIOS VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function ScenariosView({onStart}){
  const [scenarios,setScenarios]=useState([]);
  const [filter,setFilter]=useState('all');
  const [loading,setLoading]=useState(true);
  useEffect(()=>{fetch(`${API}/scenarios`).then(r=>r.json()).then(d=>{setScenarios(d);setLoading(false);});},[]);
  const cats=['all','travel','food','shopping','social','health','work','emergency'];
  const filtered=filter==='all'?scenarios:scenarios.filter(s=>s.category===filter);
  return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Scenario Practice</h2><p className="page-sub">Roleplay real-life Icelandic situations</p></div>
      </div>
      <div className="filter-row">
        {cats.map(c=>(
          <button key={c} className={`pill ${filter===c?'active':''}`} onClick={()=>setFilter(c)}>
            {c.charAt(0).toUpperCase()+c.slice(1)}
            {c!=='all'&&<span className="pill-count">{scenarios.filter(s=>s.category===c).length}</span>}
          </button>
        ))}
      </div>
      {loading&&<div className="empty-state">Loading…</div>}
      <div className="scenario-grid">
        {filtered.map(s=>(
          <div key={s.id} className="scenario-card">
            <div className="scenario-icon">{s.icon}</div>
            <div className="scenario-body">
              <div className="scenario-top">
                <h3 className="scenario-title">{s.title}</h3>
                <span className={`level-tag level-${s.level}`}>{s.level}</span>
              </div>
              <p className="scenario-desc">{s.description}</p>
              <div className="scenario-vocab">
                {s.vocabulary?.slice(0,4).map((v,i)=><span key={i} className="vocab-chip">{v}</span>)}
              </div>
              <button className="launch-btn" onClick={()=>onStart(s.id)}>Start scenario →</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// LESSONS VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function LessonsView({onStart}){
  const [lessons,setLessons]=useState([]);
  const [completed,setCompleted]=useState({});
  const [track,setTrack]=useState('beginner');
  const [loading,setLoading]=useState(true);
  useEffect(()=>{
    Promise.all([
      fetch(`${API}/lessons`).then(r=>r.json()),
      fetch(`${API}/progress`).then(r=>r.json()),
    ]).then(([ls,prog])=>{
      setLessons(ls);
      const comp={};(prog.completed_lessons||[]).forEach(id=>{comp[id]=true;});
      setCompleted(comp);setLoading(false);
    });
  },[]);
  const tracks=['beginner','intermediate','advanced'];
  const filtered=lessons.filter(l=>l.track===track).sort((a,b)=>a.order-b.order);
  return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Lesson Curriculum</h2><p className="page-sub">Structured Icelandic from the ground up</p></div>
        <div className="level-pills">
          {tracks.map(t=>(
            <button key={t} className={`pill ${track===t?'active':''}`} onClick={()=>setTrack(t)}>
              {t.charAt(0).toUpperCase()+t.slice(1)}
            </button>
          ))}
        </div>
      </div>
      {loading&&<div className="empty-state">Loading lessons…</div>}
      <div className="lesson-track">
        {filtered.map((l,idx)=>{
          const done=!!completed[l.id];
          const avail=idx===0||!!completed[filtered[idx-1]?.id];
          return(
            <div key={l.id} className={`lesson-card ${done?'done':''} ${!avail?'locked':''}`}>
              <div className="lesson-node">
                {done?'✓':avail?<span className="node-num">{l.order}</span>:'🔒'}
              </div>
              <div className="lesson-body">
                <div className="lesson-header-row">
                  <h3 className="lesson-title">{l.title}</h3>
                  {done&&<span className="done-badge">Completed</span>}
                </div>
                <p className="lesson-desc">{l.description}</p>
                <p className="lesson-grammar">Grammar: <em>{l.grammar_focus}</em></p>
                <div className="lesson-vocab">
                  {l.vocabulary?.slice(0,5).map((v,i)=><span key={i} className="vocab-chip">{v}</span>)}
                </div>
                {avail&&(
                  <button className="launch-btn" onClick={()=>onStart(l.id)}>
                    {done?'Practice again →':'Start lesson →'}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// HEATMAP VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function HeatmapView(){
  const [data,setData]=useState(null);
  const [analysis,setAnalysis]=useState(null);
  const [loading,setLoading]=useState(true);
  const [subtab,setSubtab]=useState('heatmap');
  useEffect(()=>{
    Promise.all([
      fetch(`${API}/heatmap`).then(r=>r.json()),
      fetch(`${API}/heatmap/analysis`).then(r=>r.json()),
    ]).then(([h,a])=>{setData(h);setAnalysis(a);setLoading(false);});
  },[]);
  if(loading)return<div className="page-layout"><div className="empty-state">Analysing your mistakes…</div></div>;
  const maxCount=data?Math.max(...Object.values(data.error_map||{}).map(c=>c.count||0),1):1;
  const categories=data?.by_category||{};
  const catKeys=Object.keys(categories).sort((a,b)=>categories[b]-categories[a]);
  return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Mistake Heatmap</h2><p className="page-sub">Your error patterns across all sessions</p></div>
        <div className="level-pills">
          <button className={`pill ${subtab==='heatmap'?'active':''}`} onClick={()=>setSubtab('heatmap')}>Heatmap</button>
          <button className={`pill ${subtab==='analysis'?'active':''}`} onClick={()=>setSubtab('analysis')}>AI Analysis</button>
        </div>
      </div>
      {subtab==='heatmap'&&(
        <>
          <div className="hm-section">
            <p className="hm-section-title">Error Categories</p>
            {catKeys.length===0&&<div className="empty-state">No errors recorded yet. Start practicing!</div>}
            <div className="hm-categories">
              {catKeys.map(cat=>{
                const maxCat=Math.max(...catKeys.map(k=>categories[k]),1);
                const pct=Math.round((categories[cat]/maxCat)*100);
                const heat=pct>75?'heat-5':pct>50?'heat-4':pct>30?'heat-3':pct>15?'heat-2':'heat-1';
                return(
                  <div key={cat} className="hm-cat-row">
                    <span className="hm-cat-label">{cat}</span>
                    <div className="hm-bar-outer"><div className={`hm-bar-inner ${heat}`} style={{width:`${pct}%`}}/></div>
                    <span className="hm-cat-count">{categories[cat]}</span>
                  </div>
                );
              })}
            </div>
          </div>
          {data?.error_map&&Object.keys(data.error_map).length>0&&(
            <div className="hm-section">
              <p className="hm-section-title">Error Grid <span className="hm-legend">(darker = more frequent)</span></p>
              <div className="hm-grid">
                {Object.entries(data.error_map)
                  .sort((a,b)=>b[1].count-a[1].count).slice(0,40)
                  .map(([key,val])=>{
                    const intensity=clamp(Math.round((val.count/maxCount)*5),1,5);
                    return(
                      <div key={key} className={`hm-cell heat-${intensity}`}
                        title={`"${val.original}" → "${val.correction}" (${val.count}×)\n${val.category}`}>
                        <span className="hm-cell-wrong">{val.original}</span>
                        <span className="hm-cell-count">{val.count}×</span>
                      </div>
                    );
                  })}
              </div>
            </div>
          )}
          {data?.top_errors?.length>0&&(
            <div className="hm-section">
              <p className="hm-section-title">Most Repeated Mistakes</p>
              <div className="hm-top-list">
                {data.top_errors.slice(0,8).map((e,i)=>(
                  <div key={i} className="hm-top-item">
                    <span className="hm-rank">#{i+1}</span>
                    <div className="hm-top-body">
                      <div className="error-line">
                        <span className="wrong">{e.original}</span>
                        <span className="arrow">→</span>
                        <span className="right">{e.correction}</span>
                      </div>
                      <p className="error-explain">{e.explanation}</p>
                    </div>
                    <span className="hm-top-count">{e.count}×</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
      {subtab==='analysis'&&(
        <div className="analysis-panel">
          {!analysis?.summary&&<div className="empty-state">Not enough data yet. Keep practicing!</div>}
          {analysis?.summary&&(
            <>
              <div className="analysis-block">
                <p className="block-label">AI Pattern Analysis</p>
                <p className="analysis-text">{analysis.summary}</p>
              </div>
              {analysis.top_patterns?.length>0&&(
                <div className="analysis-block">
                  <p className="block-label">Recurring Patterns</p>
                  {analysis.top_patterns.map((p,i)=>(
                    <div key={i} className="pattern-item">
                      <span className="pattern-num">{i+1}</span>
                      <div><p className="pattern-title">{p.pattern}</p><p className="pattern-desc">{p.description}</p></div>
                    </div>
                  ))}
                </div>
              )}
              {analysis.recommended_focus?.length>0&&(
                <div className="analysis-block">
                  <p className="block-label">Recommended Focus Areas</p>
                  <div className="focus-chips">
                    {analysis.recommended_focus.map((f,i)=><span key={i} className="focus-chip">{f}</span>)}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PROGRESS VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function ProgressView(){
  const [data,setData]=useState(null);const [days,setDays]=useState(30);const [loading,setLoading]=useState(true);
  useEffect(()=>{setLoading(true);fetch(`${API}/progress?days=${days}`).then(r=>r.json()).then(d=>{setData(d);setLoading(false);});},[days]);
  if(loading)return<div className="page-layout"><div className="empty-state">Loading…</div></div>;
  const totals=data?.totals||{};const daily=data?.daily||[];
  const maxTurns=Math.max(...daily.map(d=>d.turns||0),1);
  const streak=(()=>{if(!daily.length)return 0;const dates=new Set(daily.map(d=>d.date));let count=0,d=new Date();while(true){const iso=d.toISOString().slice(0,10);if(dates.has(iso)){count++;d.setDate(d.getDate()-1);}else break;}return count;})();
  return(
    <div className="page-layout">
      <div className="page-header">
        <h2 className="page-title">Your Progress</h2>
        <div className="days-toggle">{[7,30,90].map(n=><button key={n} className={`pill ${days===n?'active':''}`} onClick={()=>setDays(n)}>{n}d</button>)}</div>
      </div>
      <div className="stats-grid">
        {[{label:'Total Turns',value:totals.total_turns||0},{label:'Sessions',value:totals.total_sessions||0},
          {label:'Active Days',value:totals.active_days||0},{label:'Day Streak',value:streak,suffix:'🔥'},
          {label:'Cards Total',value:data?.cards_total||0},{label:'Cards Due',value:data?.cards_due||0,highlight:(data?.cards_due||0)>0},
        ].map((s,i)=>(
          <div key={i} className={`stat-card ${s.highlight?'highlight':''}`}>
            <div className="stat-value">{s.value}{s.suffix||''}</div>
            <div className="stat-label">{s.label}</div>
          </div>
        ))}
      </div>
      <div className="chart-section">
        <p className="chart-title">Daily Practice</p>
        <div className="bar-chart">
          {daily.length===0&&<div className="empty-state">No data yet — start practicing!</div>}
          {daily.map((d,i)=>(
            <div key={i} className="bar-col">
              <div className="bar-wrap"><div className="bar" style={{height:`${(d.turns/maxTurns)*100}%`}} title={`${d.turns} turns`}/></div>
              <span className="bar-label">{d.date?.slice(5)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// FLASHCARDS VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function FlashcardsView(){
  const [mode,setMode]=useState('browse');const [cards,setCards]=useState([]);const [dueCards,setDueCards]=useState([]);
  const [loading,setLoading]=useState(true);const [filter,setFilter]=useState('all');const [posFilter,setPosFilter]=useState('all');
  const [newIs,setNewIs]=useState('');const [newEn,setNewEn]=useState('');const [newNote,setNewNote]=useState('');const [newCat,setNewCat]=useState('vocabulary');const [newPos,setNewPos]=useState('');
  const [genTopic,setGenTopic]=useState('common greetings and everyday phrases');const [genCount,setGenCount]=useState(10);const [genLevel,setGenLevel]=useState('beginner');const [genLoading,setGenLoading]=useState(false);
  const [reviewIdx,setReviewIdx]=useState(0);const [showAns,setShowAns]=useState(false);const [revResult,setRevResult]=useState(null);
  const [fcRecording,setFcRecording]=useState(false);const [fcPronScore,setFcPronScore]=useState(null);const [fcScoring,setFcScoring]=useState(false);
  const fcMediaRecorder=useRef(null);const fcAudioChunks=useRef([]);
  const POS_LABELS=['noun','verb','adjective','adverb','preposition','conjunction','pronoun','phrase','other'];

  const loadCards=async()=>{
    setLoading(true);
    const[all,due]=await Promise.all([
      fetch(`${API}/flashcards`).then(r=>r.json()),
      fetch(`${API}/flashcards?due_only=true`).then(r=>r.json()),
    ]);
    setCards(all);setDueCards(due);setLoading(false);
  };
  useEffect(()=>{loadCards();},[]);

  const filtered=cards.filter(c=>(filter==='all'||c.category===filter)&&(posFilter==='all'||c.part_of_speech===posFilter));
  const reviewCard=dueCards[reviewIdx];

  const handleReview=async(correct)=>{
    setRevResult(correct?'correct':'incorrect');
    await fetch(`${API}/flashcards/${reviewCard.id}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({card_id:reviewCard.id,correct})});
    setTimeout(()=>{
      setShowAns(false);setRevResult(null);setFcPronScore(null);
      if(reviewIdx+1>=dueCards.length){loadCards();setReviewIdx(0);setMode('browse');}
      else setReviewIdx(i=>i+1);
    },700);
  };

  const handleAdd=async(e)=>{
    e.preventDefault();if(!newIs.trim()||!newEn.trim())return;
    await fetch(`${API}/flashcards`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({icelandic:newIs,english:newEn,notes:newNote,category:newCat,part_of_speech:newPos})});
    setNewIs('');setNewEn('');setNewNote('');setNewCat('vocabulary');setNewPos('');
    loadCards();setMode('browse');
  };

  const handleGenerate=async()=>{
    setGenLoading(true);
    await fetch(`${API}/flashcards/generate`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count:genCount,level:genLevel,topic:genTopic})});
    await loadCards();setGenLoading(false);setMode('browse');
  };

  const handleDelete=async(id)=>{
    await fetch(`${API}/flashcards/${id}`,{method:'DELETE'});
    setCards(prev=>prev.filter(c=>c.id!==id));
  };

  const startFcRecording=async()=>{
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:true});
      fcAudioChunks.current=[];
      fcMediaRecorder.current=new MediaRecorder(stream,{mimeType:'audio/webm'});
      fcMediaRecorder.current.ondataavailable=e=>{if(e.data.size>0)fcAudioChunks.current.push(e.data);};
      fcMediaRecorder.current.start();
      setFcRecording(true);setFcPronScore(null);
    }catch{alert('Microphone access denied.');}
  };

  const stopFcRecording=async(expectedText)=>{
    if(!fcMediaRecorder.current||fcMediaRecorder.current.state==='inactive')return;
    fcMediaRecorder.current.onstop=async()=>{
      fcMediaRecorder.current.stream.getTracks().forEach(t=>t.stop());
      const blob=new Blob(fcAudioChunks.current,{type:'audio/webm'});
      if(blob.size<500)return;
      setFcScoring(true);
      try{
        const form=new FormData();
        form.append('audio',blob,'rec.webm');
        form.append('expected_text',expectedText);
        const r=await fetch(`${PRONUN}/score`,{method:'POST',body:form});
        if(r.ok)setFcPronScore(await r.json());
      }catch(e){console.error('FC pron:',e);}
      finally{setFcScoring(false);}
    };
    fcMediaRecorder.current.stop();
    setFcRecording(false);
  };

  if(loading)return<div className="page-layout"><div className="empty-state">Loading…</div></div>;

  return(
    <div className="page-layout">
      <div className="page-header">
        <h2 className="page-title">Flashcards</h2>
        <div className="fc-header-actions">
          <span className="badge">{dueCards.length} due</span>
          <span className="badge badge-muted">{cards.length} total</span>
          <div className="level-pills">
            {['browse','review','add','generate'].map(m=>(
              <button key={m} className={`pill ${mode===m?'active':''}`} onClick={()=>{setMode(m);setReviewIdx(0);setShowAns(false);}}>
                {m.charAt(0).toUpperCase()+m.slice(1)}
                {m==='review'&&dueCards.length>0&&<span className="pill-badge">{dueCards.length}</span>}
              </button>
            ))}
          </div>
        </div>
      </div>

      {mode==='review'&&(
        <div className="review-area">
          {dueCards.length===0?(
            <div className="review-done">
              <div className="done-icon">✦</div><h3>All caught up!</h3><p>No cards due.</p>
              <button className="pill active" onClick={()=>setMode('browse')}>Browse cards</button>
            </div>
          ):(
            <div className={`flashcard ${showAns?'flipped':''} ${revResult||''}`}>
              <div className="fc-progress">{reviewIdx+1} / {dueCards.length}</div>
              <div className="fc-front">
                <span className="fc-category">{reviewCard?.category}</span>
                {reviewCard?.part_of_speech&&<span className="fc-pos">{reviewCard.part_of_speech}</span>}
                <div className="fc-word-row">
                  <p className="fc-word icelandic">{reviewCard?.icelandic}</p>
                  <button className="fc-play-btn" onClick={()=>playWord(reviewCard?.icelandic)} title="Listen">
                    <SpeakerIcon/>
                  </button>
                </div>
                <div className="fc-pron-row">
                  <button
                    className={`fc-mic-btn ${fcRecording?'recording':''}`}
                    onMouseDown={e=>{e.preventDefault();if(!fcRecording)startFcRecording();}}
                    onMouseUp={e=>{e.preventDefault();if(fcRecording)stopFcRecording(reviewCard?.icelandic);}}
                    onTouchStart={e=>{e.preventDefault();if(!fcRecording)startFcRecording();}}
                    onTouchEnd={e=>{e.preventDefault();if(fcRecording)stopFcRecording(reviewCard?.icelandic);}}
                    title={fcRecording?'Release to score':'Hold to speak'}
                  >
                    {fcRecording?<MicActiveIcon/>:<MicIcon/>}
                    <span>{fcRecording?'Release…':'Say it'}</span>
                  </button>
                  {fcScoring&&<span className="fc-scoring">Scoring…</span>}
                </div>
                {fcPronScore&&<PronunciationPanel score={fcPronScore}/>}
                <button className="fc-reveal-btn" onClick={()=>setShowAns(true)}>Reveal answer</button>
              </div>
              {showAns&&(
                <div className="fc-back">
                  <div className="fc-word-row">
                    <p className="fc-word icelandic">{reviewCard?.icelandic}</p>
                    <button className="fc-play-btn" onClick={()=>playWord(reviewCard?.icelandic)} title="Listen">
                      <SpeakerIcon/>
                    </button>
                  </div>
                  <p className="fc-translation">{reviewCard?.english}</p>
                  {reviewCard?.notes&&<p className="fc-notes">{reviewCard.notes}</p>}
                  <div className="fc-actions">
                    <button className="fc-btn fc-wrong" onClick={()=>handleReview(false)}><span>✗</span>Again</button>
                    <button className="fc-btn fc-correct" onClick={()=>handleReview(true)}><span>✓</span>Got it</button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {mode==='add'&&(
        <form className="add-card-form" onSubmit={handleAdd}>
          <h3 className="form-title">Add a flashcard</h3>
          <div className="form-group"><label>Icelandic</label><input value={newIs} onChange={e=>setNewIs(e.target.value)} placeholder="e.g. Góðan daginn" required/></div>
          <div className="form-group"><label>English</label><input value={newEn} onChange={e=>setNewEn(e.target.value)} placeholder="e.g. Good morning" required/></div>
          <div className="form-group"><label>Notes</label><input value={newNote} onChange={e=>setNewNote(e.target.value)} placeholder="Grammar note or example"/></div>
          <div className="form-group"><label>Category</label>
            <div className="level-pills">{['vocabulary','grammar','phrase'].map(c=><button type="button" key={c} className={`pill ${newCat===c?'active':''}`} onClick={()=>setNewCat(c)}>{c}</button>)}</div>
          </div>
          <div className="form-group"><label>Part of speech</label>
            <div className="level-pills">{POS_LABELS.map(p=><button type="button" key={p} className={`pill ${newPos===p?'active':''}`} onClick={()=>setNewPos(newPos===p?'':p)}>{p}</button>)}</div>
          </div>
          <div className="form-actions">
            <button type="button" className="pill" onClick={()=>setMode('browse')}>Cancel</button>
            <button type="submit" className="pill active">Save</button>
          </div>
        </form>
      )}

      {mode==='generate'&&(
        <div className="add-card-form">
          <h3 className="form-title">Generate cards with AI</h3>
          <div className="form-group"><label>Topic</label><input value={genTopic} onChange={e=>setGenTopic(e.target.value)}/></div>
          <div className="form-group"><label>Count</label><input type="number" min="5" max="30" value={genCount} onChange={e=>setGenCount(parseInt(e.target.value))}/></div>
          <div className="form-group"><label>Level</label>
            <div className="level-pills">{LEVELS.map(l=><button type="button" key={l} className={`pill ${genLevel===l?'active':''}`} onClick={()=>setGenLevel(l)}>{l}</button>)}</div>
          </div>
          <div className="form-actions">
            <button className="pill" onClick={()=>setMode('browse')}>Cancel</button>
            <button className="pill active" onClick={handleGenerate} disabled={genLoading}>{genLoading?'Generating…':`Generate ${genCount} cards`}</button>
          </div>
        </div>
      )}

      {mode==='browse'&&(
        <>
          <div className="filter-row">
            {['all','vocabulary','grammar','phrase'].map(f=>(
              <button key={f} className={`pill ${filter===f?'active':''}`} onClick={()=>setFilter(f)}>
                {f.charAt(0).toUpperCase()+f.slice(1)}
                <span className="pill-count">{f==='all'?cards.length:cards.filter(c=>c.category===f).length}</span>
              </button>
            ))}
          </div>
          <div className="filter-row">
            {['all',...POS_LABELS].map(p=>{
              const cnt=p==='all'?cards.length:cards.filter(c=>c.part_of_speech===p).length;
              if(p!=='all'&&cnt===0)return null;
              return(
                <button key={p} className={`pill ${posFilter===p?'active':''}`} onClick={()=>setPosFilter(p)}>
                  {p.charAt(0).toUpperCase()+p.slice(1)}
                  <span className="pill-count">{cnt}</span>
                </button>
              );
            })}
          </div>
          {filtered.length===0&&<div className="empty-state">No cards yet!</div>}
          <div className="cards-grid">
            {filtered.map(card=>(
              <div key={card.id} className="card-item">
                <div className="card-item-top">
                  <span className="fc-category">{card.category}</span>
                  {card.part_of_speech&&<span className="fc-pos">{card.part_of_speech}</span>}
                  <button className="delete-btn" onClick={()=>handleDelete(card.id)}><TrashIcon/></button>
                </div>
                <div className="card-is-row">
                  <p className="card-is icelandic">{card.icelandic}</p>
                  <button className="card-play-btn" onClick={()=>playWord(card.icelandic)} title="Pronounce">
                    <SpeakerIcon/>
                  </button>
                </div>
                <p className="card-en">{card.english}</p>
                {card.notes&&<p className="card-note">{card.notes}</p>}
                <div className="card-stats">
                  <span>{card.times_seen} seen</span>
                  <span>{card.times_correct} correct</span>
                  <span className={card.due_date<=new Date().toISOString().slice(0,10)?'due-now':''}>{card.due_date}</span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════════
// WORD OF THE DAY CARD
// ═══════════════════════════════════════════════════════════════════════════════
function WordOfDayCard(){
  const [word, setWord]       = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(()=>{
    fetch(`${API}/word-of-day`)
      .then(r=>r.json())
      .then(d=>{setWord(d);setLoading(false);})
      .catch(()=>setLoading(false));
  },[]);

  if(loading) return null;
  if(!word) return null;

  const diffColor = word.difficulty==='beginner'?'var(--aurora-g)':
                    word.difficulty==='intermediate'?'var(--gold)':'var(--aurora-p)';

  return(
    <div className={`wotd-card ${expanded?'expanded':''}`} onClick={()=>setExpanded(e=>!e)}>
      <div className="wotd-header">
        <span className="wotd-label">🇮🇸 Word of the Day</span>
        <span className="wotd-date">{new Date().toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'})}</span>
      </div>
      <div className="wotd-main">
        <span className="wotd-word icelandic">{word.word}</span>
        <span className="wotd-pos">{word.part_of_speech}</span>
        <button className="card-play-btn" onClick={e=>{e.stopPropagation();playWord(word.word);}} title="Pronounce">
          <SpeakerIcon/>
        </button>
        <span className="wotd-en">
          {word.english}
          <span className="wotd-diff" style={{color:diffColor}}>●</span>
        </span>
      </div>
      {expanded&&(
        <div className="wotd-detail">
          <p className="wotd-example-is icelandic">{word.example_is}</p>
          <p className="wotd-example-en">{word.example_en}</p>
          {word.etymology&&<p className="wotd-etymology">🔍 {word.etymology}</p>}
        </div>
      )}
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════════════════
// CEFR VIEW
// ═══════════════════════════════════════════════════════════════════════════════
const CEFR_LEVELS = ['A1','A2','B1','B2','C1','C2'];

const CEFR_COLORS = {
  A1:'#7a8aaa', A2:'#38b2e8', B1:'#3de8a0', B2:'#c9a84c', C1:'#9b7fe8', C2:'#e85050'
};
const CEFR_LABELS = {
  A1:'Beginner', A2:'Elementary', B1:'Intermediate',
  B2:'Upper-Intermediate', C1:'Advanced', C2:'Mastery'
};

function CefrView(){
  const [mode, setMode]         = useState('overview');  // overview | exam | results
  const [estimate, setEstimate] = useState(null);
  const [history,  setHistory]  = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [exam,     setExam]     = useState(null);
  const [examId,   setExamId]   = useState(null);
  const [answers,  setAnswers]  = useState({});
  const [section,  setSection]  = useState(0);
  const [result,   setResult]   = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [timer,    setTimer]    = useState(0);
  const timerRef = useRef(null);

useEffect(()=>{
    Promise.allSettled([
      fetch(`${API}/cefr/estimate`).then(r=>r.ok?r.json():null).catch(()=>null),
      fetch(`${API}/cefr/history`).then(r=>r.ok?r.json():[]).catch(()=>[]),
    ]).then(([est, hist])=>{
      setEstimate(est.value); setHistory(hist.value||[]); setLoading(false);
    });
  },[]);

  const startExam = async(targetLevel)=>{
    setGenerating(true);
    try{
      const r = await fetch(`${API}/cefr/exam/start?target_level=${targetLevel}`,{method:'POST'});
      if(!r.ok) throw new Error();
      const d = await r.json();
      setExam(d.exam); setExamId(d.exam_id); setAnswers({}); setSection(0);
      setMode('exam');
      // Start timer
      setTimer(0);
      timerRef.current = setInterval(()=>setTimer(t=>t+1), 1000);
    }catch(e){console.error(e);}
    finally{setGenerating(false);}
  };

  const submitExam = async()=>{
    setSubmitting(true);
    clearInterval(timerRef.current);
    try{
      const answerList = Object.entries(answers).map(([qid, ans])=>({
        question_id: qid, answer: ans
      }));
      const r = await fetch(`${API}/cefr/exam/${examId}/submit`,{
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({exam_id: examId, answers: answerList})
      });
      if(!r.ok) throw new Error();
      const d = await r.json();
      setResult(d.result); setMode('results');
      // Refresh estimate
      fetch(`${API}/cefr/estimate?force_refresh=true`).then(r=>r.json()).then(setEstimate);
    }catch(e){console.error(e);}
    finally{setSubmitting(false);}
  };

  const formatTime = s => `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;

  // Count answered questions
  const allQuestions = exam ? exam.sections?.flatMap(s=>s.questions)||[] : [];
  const answeredCount = Object.keys(answers).length;

  if(loading) return <div className="page-layout"><div className="empty-state">Loading CEFR data…</div></div>;

  // ── RESULTS ──────────────────────────────────────────────────────────────
  if(mode==='results' && result){
    const col = CEFR_COLORS[result.cefr_level] || 'var(--ice)';
    return(
      <div className="page-layout">
        <div className="page-header">
          <h2 className="page-title">Exam Results</h2>
          <button className="pill active" onClick={()=>{setMode('overview');}}>← Back</button>
        </div>

        <div className="cefr-result-hero" style={{borderColor:col}}>
          <div className="cefr-result-level" style={{color:col}}>{result.cefr_level}</div>
          <div className="cefr-result-label">{CEFR_LABELS[result.cefr_level]}</div>
          <div className="cefr-result-score">{result.percentage}%</div>
          <p className="cefr-result-summary">{result.summary}</p>
        </div>

        <div className="cefr-section-scores">
          {Object.entries(result.section_scores||{}).map(([skill, scores])=>(
            <div key={skill} className="cefr-skill-bar">
              <div className="csb-label">{skill.charAt(0).toUpperCase()+skill.slice(1)}</div>
              <div className="csb-track">
                <div className="csb-fill" style={{width:`${scores.percentage}%`,background:col}}/>
              </div>
              <div className="csb-pct">{scores.percentage}%</div>
            </div>
          ))}
        </div>

        {result.strengths?.length>0&&(
          <div className="cefr-feedback-block positive">
            <p className="block-label">✦ Strengths</p>
            {result.strengths.map((s,i)=><p key={i} className="cefr-fb-item">• {s}</p>)}
          </div>
        )}
        {result.weaknesses?.length>0&&(
          <div className="cefr-feedback-block errors">
            <p className="block-label">⟳ Areas to Improve</p>
            {result.weaknesses.map((w,i)=><p key={i} className="cefr-fb-item">• {w}</p>)}
          </div>
        )}
        {result.recommendations?.length>0&&(
          <div className="cefr-feedback-block tip">
            <p className="block-label">◈ Recommendations</p>
            {result.recommendations.map((r,i)=><p key={i} className="cefr-fb-item">• {r}</p>)}
          </div>
        )}

        <div className="cefr-question-review">
          <p className="block-label" style={{marginBottom:'.6rem'}}>Question Review</p>
          {result.question_scores?.map((qs,i)=>(
            <div key={i} className={`cefr-q-review ${qs.correct?'correct':'incorrect'}`}>
              <span className="cqr-icon">{qs.correct?'✓':'✗'}</span>
              <span className="cqr-pts">{qs.points_earned}/{qs.points_possible}</span>
              <span className="cqr-feedback">{qs.feedback}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ── EXAM ─────────────────────────────────────────────────────────────────
  if(mode==='exam' && exam){
    const sections = exam.sections || [];
    const currentSection = sections[section];
    if(!currentSection) return null;
    const isLast = section === sections.length - 1;
    const sectionAnswered = currentSection.questions.every(q=>answers[q.id]);

    return(
      <div className="page-layout">
        <div className="cefr-exam-header">
          <div className="cefr-exam-title">
            <span className="page-title">CEFR Exam — {exam.target_level}</span>
            <span className="cefr-timer">{formatTime(timer)}</span>
          </div>
          <div className="cefr-exam-progress">
            {sections.map((s,i)=>(
              <button key={i} className={`cefr-sec-tab ${i===section?'active':''} ${s.questions.every(q=>answers[q.id])?'done':''}`}
                onClick={()=>setSection(i)}>
                {s.type}
              </button>
            ))}
          </div>
          <div className="cefr-answered">{answeredCount}/{allQuestions.length} answered</div>
        </div>

        <div className="cefr-section-body">
          <h3 className="cefr-section-title">{currentSection.title}</h3>
          <p className="cefr-section-instructions">{currentSection.instructions}</p>

          {currentSection.questions.map((q,qi)=>(
            <div key={q.id} className={`cefr-question ${answers[q.id]?'answered':''}`}>
              <div className="cefr-q-num">Q{qi+1}</div>
              <div className="cefr-q-body">
                {q.context&&<div className="cefr-q-context icelandic">{q.context}</div>}
                <p className="cefr-q-text">{q.question}</p>

                {q.type==='multiple_choice'&&(
                  <div className="cefr-options">
                    {q.options?.map((opt,oi)=>(
                      <button key={oi}
                        className={`cefr-option ${answers[q.id]===opt?'selected':''}`}
                        onClick={()=>setAnswers(prev=>({...prev,[q.id]:opt}))}>
                        {opt}
                      </button>
                    ))}
                  </div>
                )}

                {q.type==='fill_blank'&&(
                  <input className="cefr-fill-input"
                    placeholder="Type your answer in Icelandic…"
                    value={answers[q.id]||''}
                    onChange={e=>setAnswers(prev=>({...prev,[q.id]:e.target.value}))}/>
                )}

                {q.type==='speaking'&&(
                  <div className="cefr-speaking">
                    <p className="cefr-speaking-hint">Speak your answer, then type a summary below:</p>
                    <textarea className="cefr-speaking-input"
                      placeholder="Describe your spoken answer here… (write what you said)"
                      rows={3}
                      value={answers[q.id]||''}
                      onChange={e=>setAnswers(prev=>({...prev,[q.id]:e.target.value}))}/>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        <div className="cefr-exam-footer">
          {section>0&&(
            <button className="pill" onClick={()=>setSection(s=>s-1)}>← Previous</button>
          )}
          {!isLast&&(
            <button className="pill active" onClick={()=>setSection(s=>s+1)}>
              Next section →
            </button>
          )}
          {isLast&&(
            <button className="pill active" onClick={submitExam}
              disabled={submitting||answeredCount<allQuestions.length}>
              {submitting?'Scoring…':'Submit Exam'}
            </button>
          )}
          {isLast&&answeredCount<allQuestions.length&&(
            <span className="cefr-unanswered">{allQuestions.length-answeredCount} questions unanswered</span>
          )}
        </div>
      </div>
    );
  }

  // ── OVERVIEW ─────────────────────────────────────────────────────────────
  const estCol = estimate ? (CEFR_COLORS[estimate.level]||'var(--ice)') : 'var(--muted)';
  const nextLevel = estimate?.next_level;

  return(
    <div className="page-layout">
      <div className="page-header">
        <div>
          <h2 className="page-title">CEFR Assessment</h2>
          <p className="page-sub">Common European Framework of Reference for Languages</p>
        </div>
        <button className="pill" onClick={()=>{
          fetch(`${API}/cefr/estimate?force_refresh=true`).then(r=>r.json()).then(setEstimate);
        }}>Refresh estimate</button>
      </div>

      {/* Current level card */}
      {estimate&&(
        <div className="cefr-level-card" style={{borderColor:estCol}}>
          <div className="cefr-card-left">
            <div className="cefr-big-level" style={{color:estCol}}>{estimate.level}</div>
            <div className="cefr-level-name">{CEFR_LABELS[estimate.level]}</div>
            <div className="cefr-level-type">{estimate.type==='exam'?'Exam result':'Estimated from practice'}</div>
          </div>
          <div className="cefr-card-right">
            <div className="cefr-skill-bars">
              {[
                {label:'Grammar',    val:estimate.score_grammar},
                {label:'Vocabulary', val:estimate.score_vocabulary},
                {label:'Reading',    val:estimate.score_comprehension},
                {label:'Speaking',   val:estimate.score_speaking},
              ].map(s=>(
                <div key={s.label} className="cefr-mini-bar">
                  <span className="cmb-label">{s.label}</span>
                  <div className="cmb-track">
                    <div className="cmb-fill" style={{width:`${s.val}%`,background:estCol}}/>
                  </div>
                  <span className="cmb-val">{s.val}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* CEFR scale */}
      <div className="cefr-scale">
        {Object.entries(CEFR_LABELS).map(([lvl, label])=>(
          <div key={lvl} className={`cefr-scale-item ${estimate?.level===lvl?'current':''}`}
            style={estimate?.level===lvl?{borderColor:CEFR_COLORS[lvl],background:`${CEFR_COLORS[lvl]}18`}:{}}>
            <span className="cefr-scale-lvl" style={{color:CEFR_COLORS[lvl]}}>{lvl}</span>
            <span className="cefr-scale-label">{label}</span>
            {estimate?.level===lvl&&<span className="cefr-scale-you">← you</span>}
          </div>
        ))}
      </div>

      {/* Evidence */}
      {estimate?.evidence?.length>0&&(
        <div className="cefr-evidence">
          <p className="block-label" style={{marginBottom:'.5rem'}}>Evidence</p>
          {estimate.evidence.map((e,i)=><p key={i} className="cefr-evidence-item">• {e}</p>)}
        </div>
      )}

      {/* Next level gap */}
      {estimate?.next_level_gap&&(
        <div className="cefr-next-level">
          <p className="block-label">To reach {nextLevel}</p>
          <p>{estimate.next_level_gap}</p>
        </div>
      )}

      {/* Take exam */}
      <div className="cefr-exam-launch">
        <div className="cel-header">
          <h3 className="cel-title">Take a Formal Exam</h3>
          <p className="cel-sub">20 questions · ~20 minutes · Vocabulary, Grammar, Reading & Speaking</p>
        </div>
        <div className="cel-levels">
          {CEFR_LEVELS.map(lvl=>(
            <button key={lvl} className={`cefr-level-btn ${estimate?.level===lvl?'recommended':''}`}
              style={{borderColor:CEFR_COLORS[lvl],color:CEFR_COLORS[lvl]}}
              onClick={()=>startExam(lvl)} disabled={generating}>
              {lvl}
              {estimate?.level===lvl&&<span className="celb-rec">recommended</span>}
            </button>
          ))}
        </div>
        {generating&&<p className="cefr-generating">Generating your exam… this takes ~15 seconds</p>}
      </div>

      {/* History */}
      {history.length>0&&(
        <div className="cefr-history">
          <p className="block-label" style={{marginBottom:'.6rem'}}>Assessment History</p>
          {history.slice(0,6).map((h,i)=>(
            <div key={i} className="cefr-history-item">
              <span className="chi-level" style={{color:CEFR_COLORS[h.level]}}>{h.level}</span>
              <span className="chi-type">{h.type==='exam'?'Formal exam':'Auto-estimate'}</span>
              <span className="chi-score">{h.score_overall}%</span>
              <span className="chi-date">{h.created_at?.slice(0,10)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ICONS
// ═══════════════════════════════════════════════════════════════════════════════
const ChatIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>;
const SceneIcon   =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>;
const BookIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>;
const FireIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>;
const ChartIcon   =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>;
const CardIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>;
const SpeakerIcon =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="16" height="16"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>;
const WaveIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="16" height="16"><path d="M2 12h2M6 8v8M10 5v14M14 8v8M18 10v4M22 12h-2"/></svg>;
const MicIcon     =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="20" height="20"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8"/></svg>;
const MicActiveIcon=()=><svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg>;
const SendIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>;
const PlusIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="16" height="16"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>;
const CefrIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>;
const TrashIcon   =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>;
