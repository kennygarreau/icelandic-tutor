import React, { useState, useRef, useEffect, useCallback } from 'react';
import './App.css';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { tracer, SpanStatusCode } from './telemetry';

const API     = '/api';
const WHISPER = '/whisper';
const TTS     = '/tts';
const PRONUN  = '/api/pronunciation';
const LEVELS  = ['beginner','intermediate','advanced'];

const WELCOME_MSG = {
  id:0, role:'assistant',
  icelandic:'Halló! Ég heiti Sigríður og ég er kennarinn þinn í íslensku. Hvernig hefur þú það í dag?',
  correction:{errors:[],positive:"Welcome! I'm ready to help you learn Icelandic.",
    tip:'Try: "Mér líður vel, takk!" (I\'m doing well, thanks!)'},
};

function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v));}
function levenshtein(a,b){
  if(a===b)return 0;
  const m=a.length,n=b.length;
  const row=Array.from({length:n+1},(_,i)=>i);
  for(let i=1;i<=m;i++){
    let prev=row[0];row[0]=i;
    for(let j=1;j<=n;j++){const t=row[j];row[j]=a[i-1]===b[j-1]?prev:1+Math.min(prev,row[j],row[j-1]);prev=t;}
  }
  return row[n];
}

let _launchChat = null;
function launchChat(mode,id){_launchChat?.(mode,id);}
let _goToTab = null;
function goToTab(id){_goToTab?.(id);}
let _seedChatInput = null;
function seedChatInput(text){_seedChatInput?.(text);}

let _sessionsCache = null;
let _sessionsCacheTs = 0;
const SESSIONS_CACHE_TTL = 30_000;
function invalidateSessionsCache(){ _sessionsCache = null; _sessionsCacheTs = 0; }

let _ttsSpeed=parseFloat(localStorage.getItem('tts_speed')||'0.85');

const playWord=async(text)=>{
  try{
    const r=await fetch(`${TTS}/synthesize`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,speed:_ttsSpeed})});
    if(!r.ok)return;
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const audio=new Audio(url);
    audio.onended=()=>URL.revokeObjectURL(url);
    await audio.play();
  }catch(e){console.error(e);}
};

// ═══════════════════════════════════════════════════════════════════════════════
// WELCOME MODAL
// ═══════════════════════════════════════════════════════════════════════════════
function WelcomeModal({data, onClose, onNavigate}){
  const hour=new Date().getHours();
  const isNew=data.streak===0&&data.lessons_completed===0&&data.vocab_due===0;
  const greeting=isNew?'Halló!':hour<12?'Góðan morgun!':hour<18?'Góðan daginn!':'Gott kvöld!';
  const fmtCat=id=>id?.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase())||'';

  const today=new Date();
  const weekDots=Array.from({length:7},(_,i)=>{
    const d=new Date(today); d.setDate(today.getDate()-6+i);
    const iso=d.toISOString().slice(0,10);
    const labels=['S','M','T','W','T','F','S'];
    return{label:labels[d.getDay()],active:data.active_dates?.includes(iso),isToday:i===6};
  });

  return(
    <div className="welcome-overlay" onClick={onClose}>
      <div className="welcome-modal" onClick={e=>e.stopPropagation()}>
        <button className="welcome-close" onClick={onClose}>✕</button>

        <div className="welcome-header">
          <h2 className="welcome-greeting">{greeting}</h2>
          <div className="welcome-streak">
            <span className="welcome-streak-flame">🔥</span>
            <span className="welcome-streak-num">{data.streak}</span>
            <span className="welcome-streak-label">{data.streak===1?'day streak':'day streak'}</span>
          </div>
        </div>

        <div className="welcome-week">
          {weekDots.map((d,i)=>(
            <div key={i} className={`welcome-dot-col${d.isToday?' today':''}`}>
              <div className={`welcome-dot${d.active?' active':''}`}/>
              <span className="welcome-dot-label">{d.label}</span>
            </div>
          ))}
        </div>

        {data.word_of_day&&(
          <div className="welcome-wotd">
            <span className="welcome-wotd-tag">Word of the Day</span>
            <div className="welcome-wotd-row">
              <span className="welcome-wotd-is icelandic">{data.word_of_day.word}</span>
              <span className="welcome-wotd-en">{data.word_of_day.english}</span>
            </div>
            {data.word_of_day.example_is&&(
              <p className="welcome-wotd-ex icelandic">"{data.word_of_day.example_is}"</p>
            )}
          </div>
        )}

        <div className="welcome-actions">
          {(data.vocab_due>0||data.sentences_due>0)&&(
            <button className="welcome-action" onClick={()=>onNavigate('flashcards')}>
              <span className="welcome-action-icon">🃏</span>
              <div className="welcome-action-body">
                <span className="welcome-action-title">{data.vocab_due+data.sentences_due} cards due for review</span>
                <span className="welcome-action-sub">Keep your streak going</span>
              </div>
              <span className="welcome-action-arrow">→</span>
            </button>
          )}
          {data.next_lesson&&(
            <button className="welcome-action" onClick={()=>onNavigate('chat','lesson',data.next_lesson.id)}>
              <span className="welcome-action-icon">📖</span>
              <div className="welcome-action-body">
                <span className="welcome-action-title">{data.next_lesson.title}</span>
                <span className="welcome-action-sub">{data.next_lesson.track} · next lesson</span>
              </div>
              <span className="welcome-action-arrow">→</span>
            </button>
          )}
          {data.weak_category&&(
            <button className="welcome-action" onClick={()=>onNavigate('drill')}>
              <span className="welcome-action-icon">🎯</span>
              <div className="welcome-action-body">
                <span className="welcome-action-title">Drill: {fmtCat(data.weak_category)}</span>
                <span className="welcome-action-sub">Your weakest area this month</span>
              </div>
              <span className="welcome-action-arrow">→</span>
            </button>
          )}
        </div>

        <div className="welcome-footer">
          <div className="welcome-stats">
            {data.cefr_level&&<span className="welcome-stat">CEFR <strong>{data.cefr_level}</strong></span>}
            <span className="welcome-stat">{data.lessons_completed}/{data.lessons_total} lessons</span>
          </div>
          <button className="welcome-free-btn" onClick={()=>onNavigate('chat')}>
            {isNew?'Start learning →':'Free chat →'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ROOT
// ═══════════════════════════════════════════════════════════════════════════════
export default function App(){
  const [tab,setTab]=useState('chat');
  const [showWelcome,setShowWelcome]=useState(false);
  const [dashData,setDashData]=useState(null);
  const [speed,setSpeed]=useState(_ttsSpeed);
  const handleSpeedChange=(v)=>{_ttsSpeed=v;setSpeed(v);localStorage.setItem('tts_speed',v);};
  const TABS=[
    {id:'chat',      icon:<ChatIcon/>,  label:'Chat'},
    {id:'lessons',   icon:<BookIcon/>,  label:'Lessons'},
    {id:'drill',     icon:<DrillIcon/>, label:'Drill'},
    {id:'flashcards',   icon:<CardIcon/>,   label:'Cards'},
    {id:'library',      icon:<LibraryIcon/>,label:'Library'},
    {id:'map',          icon:<MapIcon/>,    label:'Map'},
    {id:'pronunciation',icon:<PronIcon/>,  label:'Pronunciation'},
    {id:'progress',  icon:<ChartIcon/>, label:'Progress'},
  ];
  const goChat=(mode,id)=>{setTab('chat'); setTimeout(()=>launchChat(mode,id),50);};
  useEffect(()=>{_goToTab=setTab;return()=>{_goToTab=null;};},[]);

  useEffect(()=>{
    const today=new Date().toISOString().slice(0,10);
    if(localStorage.getItem('last_welcome_date')!==today){
      fetch(`${API}/dashboard`).then(r=>r.json()).then(d=>{
        setDashData(d);
        setShowWelcome(true);
        localStorage.setItem('last_welcome_date',today);
      }).catch(()=>{});
    }
  },[]);

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
        <div className="sidebar-speed">
          <SpeakerIcon/>
          <input type="range" min="0.5" max="1.5" step="0.05" value={speed}
            onChange={e=>handleSpeedChange(parseFloat(e.target.value))}/>
          <span className="sidebar-speed-val">{speed.toFixed(2)}×</span>
        </div>
      </nav>
      <main className="main">
        {tab==='chat'       && <ChatView/>}
        {tab==='lessons'    && <LessonsShell onStart={(mode,id)=>goChat(mode,id)}/>}
        {tab==='drill'      && <DrillView/>}
        {tab==='pronunciation' && <PronunciationView/>}
        {tab==='progress'   && <ProgressShell/>}
        {tab==='flashcards' && <FlashcardsView/>}
        {tab==='library'    && <LibraryView/>}
        {tab==='map'        && <MapView/>}
      </main>
      <nav className="bottom-nav" id="bottom-nav" style={{display:'none'}}>
        {TABS.map(t=>(
          <button key={t.id} className={`bottom-nav-btn ${tab===t.id?'active':''}`} onClick={()=>setTab(t.id)}>
            {t.icon}<span>{t.label}</span>
          </button>
        ))}
      </nav>

      {showWelcome&&dashData&&(
        <WelcomeModal data={dashData} onClose={()=>setShowWelcome(false)}
          onNavigate={(tabId,mode,id)=>{
            setShowWelcome(false);
            setTab(tabId);
            if(mode&&id) setTimeout(()=>launchChat(mode,id),80);
          }}/>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function ChatView(){
  const [messages,  setMessages]  = useState([WELCOME_MSG]);
  const [sessionId,    setSessionId]    = useState(null);
  const [sessionTitle, setSessionTitle] = useState(null);
  const [level,        setLevel]        = useState('beginner');
  const [loading,      setLoading]      = useState(false);
  const [playingId,    setPlayingId]    = useState(null);
  const [correction,   setCorrection]   = useState(WELCOME_MSG.correction);
  const [newVocab,     setNewVocab]     = useState([]);
  const [autoPlay,     setAutoPlay]     = useState(true);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [chatMode,  setChatMode]  = useState({mode:'free',id:null,label:''});
  const [pronScore, setPronScore] = useState(null);
  const [shownTranslations, setShownTranslations] = useState({});
  const [lessonComplete, setLessonComplete] = useState(null);
  const [pdfModal,      setPdfModal]       = useState(null); // {url, title}

  const [drawerOpen,      setDrawerOpen]      = useState(false);
  const [pastSessions,    setPastSessions]    = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [renamingId,      setRenamingId]      = useState(null);
  const [renameValue,     setRenameValue]     = useState('');

  const chatEndRef     = useRef(null);
  const inputRef       = useRef(null);
  const currentAudioRef= useRef(null);
  const stateRef       = useRef({});

  useEffect(()=>{
    _launchChat=(mode,id)=>{
      setChatMode({mode,id,label:''});
      setMessages((mode==='lesson'||mode==='scenario')?[]:[WELCOME_MSG]); setSessionId(null); setNewVocab([]); setLessonComplete(null);
      if(mode==='scenario') fetch(`${API}/scenarios/${id}`).then(r=>r.json()).then(s=>setChatMode(c=>({...c,label:`🎭 ${s.title}`,scenarioData:s})));
      if(mode==='lesson')   fetch(`${API}/lessons/${id}`).then(r=>r.json()).then(l=>setChatMode(c=>({...c,label:`📖 ${l.title}`,lessonData:l})));
    };
    _seedChatInput=(text)=>{setInput(text);setTimeout(()=>inputRef.current?.focus(),80);};
    return()=>{_launchChat=null;_seedChatInput=null;};
  },[]);

  useEffect(()=>{chatEndRef.current?.scrollIntoView({behavior:'smooth'});},[messages]);

  const speakText=useCallback(async(text,msgId)=>{
    setPlayingId(msgId);
    try{
      const r=await fetch(`${TTS}/synthesize`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,speed:_ttsSpeed})});
      if(!r.ok)throw new Error();
      const blob=await r.blob();const url=URL.createObjectURL(blob);
      const audio=new Audio(url);
      currentAudioRef.current=audio;
      audio.onended=()=>{setPlayingId(null);URL.revokeObjectURL(url);currentAudioRef.current=null;};
      audio.onerror=()=>{setPlayingId(null);URL.revokeObjectURL(url);currentAudioRef.current=null;};
      await audio.play();
    }catch{setPlayingId(null);}
  },[]);

  // Always-current snapshot — sendMessage reads from here so it needs zero deps
  stateRef.current={messages,level,autoPlay,sessionId,chatMode,speakText};

  const loadPastSessions = async (limit=30, force=false) => {
    const now = Date.now();
    if (!force && _sessionsCache && now - _sessionsCacheTs < SESSIONS_CACHE_TTL) {
      setPastSessions(_sessionsCache);
      return;
    }
    setSessionsLoading(true);
    try{
      const d=await fetch(`${API}/sessions?limit=${limit}`).then(r=>r.json());
      _sessionsCache=d; _sessionsCacheTs=Date.now();
      setPastSessions(d);
    }
    catch(e){ console.error(e); }
    finally{ setSessionsLoading(false); }
  };

  const openDrawer = () => { loadPastSessions(); setDrawerOpen(true); };

  const loadSession = async (sid) => {
    try{
      const data = await fetch(`${API}/sessions/${sid}`).then(r=>r.json());
      const hydrated = data.messages.map(m => {
        if(m.role==='user') return {id:m.id, role:'user', text:m.content};
        return {id:m.id, role:'assistant', icelandic:m.icelandic||'',
                correction:m.correction||null,
                english_translation:m.correction?.english_translation||''};
      });
      setMessages(hydrated.length ? hydrated : [WELCOME_MSG]);
      setSessionId(sid);
      setSessionTitle(data.title||null);
      setLevel(data.level||'beginner');
      const lastAsst = [...hydrated].reverse().find(m=>m.role==='assistant');
      setCorrection(lastAsst?.correction || WELCOME_MSG.correction);
      setNewVocab([]); setPronScore(null); setLessonComplete(null);
      if(data.mode==='scenario' && data.scenario_id){
        const sc = await fetch(`${API}/scenarios/${data.scenario_id}`).then(r=>r.json());
        setChatMode({mode:'scenario', id:data.scenario_id, label:`🎭 ${sc.title}`, scenarioData:sc});
      } else if(data.mode==='lesson' && data.lesson_id){
        const ls = await fetch(`${API}/lessons/${data.lesson_id}`).then(r=>r.json());
        setChatMode({mode:'lesson', id:data.lesson_id, label:`📖 ${ls.title}`, lessonData:ls});
      } else {
        setChatMode({mode:'free', id:null, label:''});
      }
      setDrawerOpen(false);
    }catch(e){ console.error(e); }
  };

  const renameSession = async (sid, title) => {
    if(!title.trim()){ setRenamingId(null); return; }
    try{
      await fetch(`${API}/sessions/${sid}`,{method:'PATCH',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({title:title.trim()})});
      setPastSessions(prev=>prev.map(s=>s.id===sid?{...s,title:title.trim()}:s));
    }catch(e){ console.error(e); }
    setRenamingId(null);
  };

  const handleDeleteSession = async (sid, e) => {
    e.stopPropagation();
    try{
      await fetch(`${API}/sessions/${sid}`,{method:'DELETE'});
      invalidateSessionsCache();
      setPastSessions(prev=>prev.filter(s=>s.id!==sid));
      if(stateRef.current.sessionId===sid) newSession();
    }catch(e){ console.error(e); }
  };

  const beginLesson=(lessonId,lessonTitle)=>{
    setChatMode(c=>({...c,mode:'lesson',id:lessonId}));
    setMessages([]);
    setLoading(true);
    const streamId=Date.now()+1;
    const level=stateRef.current.level;
    fetch(`${API}/chat/stream`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:null,messages:[{role:'user',content:'Kenndu mér efni þessarar kennslustundar.'}],level,mode:'lesson',lesson_id:lessonId})})
    .then(async resp=>{
      if(!resp.ok){setLoading(false);return;}
      const reader=resp.body.getReader();const decoder=new TextDecoder();let buf='';let started=false;
      setMessages([{id:streamId,role:'assistant',icelandic:'',streaming:true}]);
      while(true){
        const{done,value}=await reader.read();if(done)break;
        buf+=decoder.decode(value,{stream:true});
        const parts=buf.split('\n\n');buf=parts.pop();
        for(const part of parts){
          if(!part.startsWith('data:'))continue;
          try{
            const evt=JSON.parse(part.slice(5).trim());
            if(evt.t==='tok'){
              setMessages(prev=>prev.map(m=>m.id===streamId
                ?{...m,icelandic:(started?m.icelandic:'')+evt.v,streaming:true}:m));
              started=true;
            } else if(evt.t==='tts_ready'){
              if(stateRef.current.autoPlay) stateRef.current.speakText(evt.icelandic,streamId);
            } else if(evt.t==='done'){
              setMessages(prev=>prev.map(m=>m.id===streamId
                ?{...m,icelandic:evt.icelandic,english_translation:evt.english_translation,
                  correction:evt.english_correction,lesson_progress:evt.lesson_progress,
                  rag_sources:evt.rag_sources||[],streaming:false}:m));
              if(!stateRef.current.sessionId) setSessionId(evt.session_id);
            }
          }catch{}
        }
      }
      setMessages(prev=>prev.map(m=>m.id===streamId&&m.streaming?{...m,streaming:false}:m));
    })
    .finally(()=>setLoading(false));
  };

  const beginScenario=(scenarioId)=>{
    setChatMode(c=>({...c,mode:'scenario',id:scenarioId}));
    setMessages([]);
    setLoading(true);
    const streamId=Date.now()+1;
    const level=stateRef.current.level;
    fetch(`${API}/chat/stream`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:null,messages:[{role:'user',content:'Byrjum!'}],level,mode:'scenario',scenario_id:scenarioId})})
    .then(async resp=>{
      if(!resp.ok){setLoading(false);return;}
      const reader=resp.body.getReader();const decoder=new TextDecoder();let buf='';let started=false;
      setMessages([{id:streamId,role:'assistant',icelandic:'',streaming:true}]);
      while(true){
        const{done,value}=await reader.read();if(done)break;
        buf+=decoder.decode(value,{stream:true});
        const parts=buf.split('\n\n');buf=parts.pop();
        for(const part of parts){
          if(!part.startsWith('data:'))continue;
          try{
            const evt=JSON.parse(part.slice(5).trim());
            if(evt.t==='tok'){
              setMessages(prev=>prev.map(m=>m.id===streamId
                ?{...m,icelandic:(started?m.icelandic:'')+evt.v,streaming:true}:m));
              started=true;
            } else if(evt.t==='tts_ready'){
              if(stateRef.current.autoPlay) stateRef.current.speakText(evt.icelandic,streamId);
            } else if(evt.t==='done'){
              setMessages(prev=>prev.map(m=>m.id===streamId
                ?{...m,icelandic:evt.icelandic,english_translation:evt.english_translation,
                  correction:evt.english_correction,lesson_progress:evt.lesson_progress,
                  rag_sources:evt.rag_sources||[],streaming:false}:m));
              if(!stateRef.current.sessionId) setSessionId(evt.session_id);
            }
          }catch{}
        }
      }
      setMessages(prev=>prev.map(m=>m.id===streamId&&m.streaming?{...m,streaming:false}:m));
    })
    .finally(()=>setLoading(false));
  };

  const sendMessage=useCallback(async(text,audioBlob=null)=>{
    const{messages,level,autoPlay,sessionId,chatMode,speakText}=stateRef.current;
    const userText=text.trim();if(!userText)return;
    const isNewSession=!sessionId;
    const userMsg={id:Date.now(),role:'user',text:userText};
    const nextMessages=[...messages,userMsg];
    setMessages(nextMessages);setLoading(true);setPronScore(null);
    if(audioBlob) scorePronunciation(audioBlob,userText);

    const history=nextMessages.filter(m=>m.role==='user'||m.role==='assistant')
      .map(m=>({role:m.role,content:m.role==='user'?m.text:m.icelandic}));


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
          } else if(evt.t==='tts_ready'){
            if(autoPlay) speakText(evt.icelandic,streamId);
          } else if(evt.t==='done'){
            _span.addEvent('stream_done',{'total_ms':Date.now()-_t0});
            _span.setStatus({code:SpanStatusCode.OK});
            if(!sessionId) setSessionId(evt.session_id);
            if(isNewSession && evt.session_id){
              invalidateSessionsCache();
              fetch(`${API}/sessions/${evt.session_id}/generate-title`,{method:'POST'})
                .then(r=>r.json())
                .then(d=>{
                  setSessionTitle(d.title);
                  setPastSessions(prev=>prev.map(s=>s.id===evt.session_id?{...s,title:d.title}:s));
                })
                .catch(()=>{});
            }
            setCorrection(evt.english_correction);
            setNewVocab(evt.new_vocabulary||[]);
            setMessages(prev=>prev.map(m=>m.id===streamId
              ?{...m,icelandic:evt.icelandic,english_translation:evt.english_translation,
                correction:evt.english_correction,lesson_progress:evt.lesson_progress,
                rag_sources:evt.rag_sources||[],streaming:false}
              :m));
            if(evt.lesson_just_completed) setLessonComplete(stateRef.current.chatMode);
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


  const scorePronunciation=async(blob,spokenText)=>{
    const _span=tracer.startSpan('pronunciation.score');
    try{
      const form=new FormData();
      form.append('audio',blob,'rec.webm');
      form.append('spoken_text',spokenText);
      form.append('translate','1');
      if(stateRef.current.sessionId) form.append('session_id',stateRef.current.sessionId);
      const r=await fetch(`${PRONUN}/score`,{method:'POST',body:form});
      if(!r.ok){_span.setStatus({code:SpanStatusCode.ERROR});return;}
      const result=await r.json();
      _span.setAttributes({'score.overall':result.overall_score??0});
      _span.setStatus({code:SpanStatusCode.OK});
      setPronScore(result);
    }catch(e){_span.setStatus({code:SpanStatusCode.ERROR});console.error('Pron:',e);}
    finally{_span.end();}
  };

  const toggleTranslation=id=>setShownTranslations(prev=>({...prev,[id]:!prev[id]}));
  const newSession=()=>{
    setMessages([WELCOME_MSG]);setSessionId(null);setSessionTitle(null);setCorrection(WELCOME_MSG.correction);
    setNewVocab([]);setPronScore(null);setChatMode({mode:'free',id:null,label:''});
  };

  const isStreaming=messages.some(m=>m.streaming);

  return(
    <div className="chat-layout">
      <div className="chat-col">
        {drawerOpen&&(
          <>
            <div className="sessions-backdrop" onClick={()=>setDrawerOpen(false)}/>
            <div className="sessions-drawer">
              <div className="sessions-drawer-header">
                <span className="sessions-drawer-title">Recent Chats</span>
                <button className="sessions-drawer-close" onClick={()=>setDrawerOpen(false)}>✕</button>
              </div>
              <div className="sessions-list">
                {sessionsLoading&&<div className="empty-state">Loading…</div>}
                {!sessionsLoading&&pastSessions.length===0&&<div className="empty-state">No past sessions yet.</div>}
                {pastSessions.map(s=>(
                  <div key={s.id} className={`session-item ${s.id===sessionId?'active':''}`}
                       onClick={()=>renamingId!==s.id&&loadSession(s.id)}>
                    <div className="session-item-body">
                      {renamingId===s.id?(
                        <input className="session-rename-input" value={renameValue} autoFocus
                          onChange={e=>setRenameValue(e.target.value)}
                          onBlur={()=>renameSession(s.id,renameValue)}
                          onKeyDown={e=>{
                            if(e.key==='Enter') renameSession(s.id,renameValue);
                            if(e.key==='Escape') setRenamingId(null);
                            e.stopPropagation();
                          }}
                          onClick={e=>e.stopPropagation()}
                        />
                      ):(
                        <span className="session-item-title"
                          onDoubleClick={e=>{e.stopPropagation();setRenamingId(s.id);setRenameValue(s.title||'');}}
                          title="Double-click to rename">
                          {s.title||'Untitled session'}
                        </span>
                      )}
                      <div className="session-item-meta">
                        {s.mode!=='free'&&<span className="session-mode-badge">{s.mode}</span>}
                        <span className="session-date">{s.updated_at?.slice(0,10)}</span>
                        <span className="session-turns">{s.turn_count} turns</span>
                      </div>
                      {s.last_icelandic&&renamingId!==s.id&&(
                        <p className="session-preview">{s.last_icelandic.slice(0,70)}{s.last_icelandic.length>70?'…':''}</p>
                      )}
                    </div>
                    <button className="session-delete-btn" onClick={e=>handleDeleteSession(s.id,e)} title="Delete">✕</button>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
        <div className="chat-topbar">
          <div className="chat-topbar-left">
            <button className="history-btn" onClick={openDrawer} title="Chat history"><HistoryIcon/></button>
            <span className="topbar-title">{sessionTitle||'Conversation'}</span>
            {chatMode.label&&<span className="mode-badge">{chatMode.label}</span>}
            {sessionId&&!chatMode.label&&<span className="session-badge">Active</span>}
          </div>
          <div className="chat-topbar-right">
            {(!chatMode.mode||chatMode.mode==='free')&&(
              <div className="topbar-difficulty">
                <span className="topbar-difficulty-label">Difficulty</span>
                <div className="level-pills">
                  {LEVELS.map(l=>(
                    <button key={l} className={`pill ${level===l?'active':''}`} onClick={()=>setLevel(l)}>
                      <span className="level-full">{l.charAt(0).toUpperCase()+l.slice(1)}</span>
                      <span className="level-abbr">{l.charAt(0).toUpperCase()+l.slice(1,3)}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            <button className="feedback-toggle-btn" onClick={()=>setFeedbackOpen(o=>!o)}>
              Feedback{(correction?.errors?.length>0||correction?.tip)&&<span className="feedback-dot"/>}
            </button>
            <button className="new-chat-btn" onClick={newSession} title="Start a new conversation">New Chat</button>
          </div>
        </div>

        {chatMode.mode==='lesson'&&chatMode.lessonData&&messages.length===0&&!loading&&(
          <div className="lesson-intro-card">
            <div className="lic-track">{chatMode.lessonData.track} · lesson {chatMode.lessonData.order}</div>
            <h2 className="lic-title">{chatMode.lessonData.title}</h2>
            <p className="lic-grammar"><em>Grammar:</em> {chatMode.lessonData.grammar_focus}</p>
            <p className="lic-goal"><em>Goal:</em> {chatMode.lessonData.goal}</p>
            {chatMode.lessonData.vocabulary?.length>0&&(
              <div className="lic-vocab">
                {chatMode.lessonData.vocabulary.slice(0,6).map((v,i)=><span key={i} className="vocab-chip">{v}</span>)}
              </div>
            )}
            <button className="lic-begin-btn" onClick={()=>beginLesson(chatMode.id,chatMode.lessonData.title)}>
              Begin lesson →
            </button>
          </div>
        )}
        {chatMode.mode==='scenario'&&chatMode.scenarioData&&messages.length===0&&!loading&&(
          <div className="lesson-intro-card">
            <div className="lic-track">{chatMode.scenarioData.category} · {chatMode.scenarioData.level}</div>
            <h2 className="lic-title">{chatMode.scenarioData.icon} {chatMode.scenarioData.title}</h2>
            <p className="lic-grammar"><em>Sigríður plays:</em> {chatMode.scenarioData.sigridur_role}</p>
            <p className="lic-goal">{chatMode.scenarioData.description}</p>
            {chatMode.scenarioData.vocabulary?.length>0&&(
              <div className="lic-vocab">
                {chatMode.scenarioData.vocabulary.slice(0,6).map((v,i)=><span key={i} className="vocab-chip">{v}</span>)}
              </div>
            )}
            <button className="lic-begin-btn" onClick={()=>beginScenario(chatMode.id)}>
              Begin scenario →
            </button>
          </div>
        )}
        {chatMode.mode==='free'&&<WordOfDayCard/>}

        {lessonComplete&&(
          <div className="lesson-complete-banner">
            <span className="lcb-star">✦</span>
            <div className="lcb-text">
              <strong>Lesson complete!</strong>
              <span>{lessonComplete.label?.replace('📖 ','')}</span>
            </div>
            <button className="lcb-dismiss" onClick={()=>setLessonComplete(null)}>✕</button>
          </div>
        )}
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
                    {chatMode.mode==='lesson'&&msg.lesson_progress&&<LessonProgressBar progress={msg.lesson_progress} title={chatMode.lessonData?.title}/>}
                    {!msg.streaming&&msg.rag_sources&&msg.rag_sources.length>0&&(
                      <div className="rag-sources">
                        {msg.rag_sources.map((s,i)=>{
                          const titles={'complete_icelandic':'Complete Icelandic',
                            'colloquial-icelandic-the-complete-course-for-beginners':'Colloquial Icelandic'};
                          const title=titles[s.source]||s.source;
                          const url=`/rag/pdfs/${s.source}.pdf`;
                          return(
                            <button key={i} className="rag-source-link"
                              title={`Relevance: ${(s.relevance*100).toFixed(0)}%`}
                              onClick={()=>setPdfModal({url,source:`${s.source}.pdf`,pageNum:s.page_number||null,title:`${title}${s.page_number?`, p.${s.page_number}`:''}` })}>
                              📖 {title}{s.page_number?`, p.${s.page_number}`:''}
                            </button>
                          );
                        })}
                      </div>
                    )}
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
          inputRef={inputRef}
          currentAudioRef={currentAudioRef}
          onStopAudio={()=>setPlayingId(null)}
        />
      </div>

      {feedbackOpen&&<div className="feedback-overlay" onClick={()=>setFeedbackOpen(false)}/>}
      <div className={`feedback-col${feedbackOpen?' feedback-open':''}`}>
        <div className="feedback-header">
          <h2>Feedback</h2>
          <button className="feedback-close-btn" onClick={()=>setFeedbackOpen(false)}>✕</button>
        </div>
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
          <button className="footer-label footer-label-link" onClick={()=>goToTab('pronunciation')}>Pronunciation ›</button>
          <div className="phoneme-grid">
            {[['Þ','þ','th in "think"'],['Ð','ð','th in "this"'],['Æ','æ','eye'],['Ö','ö','u in "burn"'],
              ['Á','á','ow in "cow"'],['É','é','ye'],['Í','í','ee'],['Ú','ú','oo']].map(([upper,lower,hint])=>(
              <div key={upper} className="phoneme">
                <span className="ph-pair">
                  <span className="ph-char">{upper}</span>
                  <span className="ph-char-lower">{lower}</span>
                </span>
                <span className="ph-hint">{hint}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      {pdfModal&&(
        <div className="pdf-modal-overlay" onClick={()=>setPdfModal(null)}>
          <div className="pdf-modal" onClick={e=>e.stopPropagation()}>
            <div className="pdf-modal-header">
              <span className="pdf-modal-title">📖 {pdfModal.title}</span>
              <button className="pdf-modal-close" onClick={()=>setPdfModal(null)}>✕</button>
            </div>
            {pdfModal.pageNum ? (
              <div className="pdf-page-view">
                <img className="pdf-page-img"
                  src={`/rag/pdfs/${pdfModal.source}/page/${pdfModal.pageNum}`}
                  alt={pdfModal.title}/>
                <a className="pdf-open-link" href={pdfModal.url} target="_blank" rel="noopener noreferrer">
                  Open full PDF ↗
                </a>
              </div>
            ) : (
              <iframe className="pdf-modal-frame" src={pdfModal.url} title={pdfModal.title}/>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT INPUT — isolated so keystrokes never re-render the message list
// ═══════════════════════════════════════════════════════════════════════════════
const ChatInput=React.memo(function ChatInput({loading,onSend,autoPlay,onAutoPlayChange,inputRef,currentAudioRef,onStopAudio}){
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
      setRecording(true);
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
      mediaRecorder.current.addEventListener('stop',()=>{
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
        <div className="pron-header-left">
          <span className="block-label">🎙 Pronunciation</span>
          {score.grade&&<span className="pron-grade">{score.grade}</span>}
        </div>
        <div className="pron-score-circle" style={{color:col,borderColor:col}}>
          <span className="pron-pct">{pct}</span>
          <span className="pron-pct-label">%</span>
        </div>
      </div>
      {score.spoken_text&&(
        <div className="pron-heard-row">
          <span className="pron-heard-is icelandic">"{score.spoken_text}"</span>
          {score.spoken_english&&(
            <span className="pron-heard-en">→ {score.spoken_english}</span>
          )}
        </div>
      )}
      {score.word_scores?.length>0&&(
        <div className="pron-words">
          {score.word_scores.map((w,i)=>{
            const wp=Math.round(w.score||0);
            const wc=wp>=80?'good':wp>=55?'ok':'bad';
            const clarityMode=w.expected===w.spoken;
            const tooltip=clarityMode
              ?`Clarity: ${wp}%`
              :w.spoken
                ?`Heard: "${w.spoken}" — ${wp}%`
                :`Word not heard (${w.status||'missing'})`;
            return(
              <div key={i} className={`pron-word pron-${wc}`} title={tooltip}>
                <span className="pron-word-text">{w.expected}</span>
                <span className="pron-word-pct">{wp}%</span>
              </div>
            );
          })}
        </div>
      )}
      {score.phoneme_tips?.length>0&&(
        <div className="pron-issues">
          {score.phoneme_tips.slice(0,2).map((p,i)=>(
            <p key={i} className="pron-issue">• {p.tip}</p>
          ))}
        </div>
      )}
    </div>
  );
}

function LessonProgressBar({progress, title}){
  if(!progress?.goal_percent) return null;
  const pct=clamp(progress.goal_percent,0,100);
  return(
    <div className="lesson-progress-bar">
      <div className="lpb-fill" style={{width:`${pct}%`}}/>
      <span className="lpb-label">
        {title?`${title} — ${pct}%`:`Lesson goal: ${pct}%`}
      </span>
      {progress.goal_note&&<span className="lpb-note">{progress.goal_note}</span>}
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
function LessonsShell({onStart}){
  const [sub,setSub]=useState('lessons');
  return(
    <div style={{display:'flex',flexDirection:'column',flex:1,overflow:'hidden'}}>
      <div className="fc-section-tabs">
        <button className={`fc-section-tab ${sub==='lessons'?'active':''}`}   onClick={()=>setSub('lessons')}>Lessons</button>
        <button className={`fc-section-tab ${sub==='scenarios'?'active':''}`} onClick={()=>setSub('scenarios')}>Scenarios</button>
      </div>
      {sub==='lessons'   &&<LessonsView   onStart={(id)=>onStart('lesson',id)}/>}
      {sub==='scenarios' &&<ScenariosView onStart={(id)=>onStart('scenario',id)}/>}
    </div>
  );
}

function LessonsView({onStart}){
  const [lessons,setLessons]=useState([]);
  const [completed,setCompleted]=useState({});
  const [track,setTrack]=useState('beginner');
  const [loading,setLoading]=useState(true);
  const loadLessons=()=>{
    fetch(`${API}/lessons`).then(r=>r.json()).then(ls=>{
      setLessons(ls);
      const comp={};ls.forEach(l=>{if(l.completed)comp[l.id]=true;});
      setCompleted(comp);setLoading(false);
    });
  };
  useEffect(()=>{loadLessons();},[]);
  const tracks=['beginner','intermediate','advanced','cultural'];
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
                <p className="lesson-goal">Goal: {l.goal}</p>
                <div className="lesson-vocab">
                  {l.vocabulary?.slice(0,5).map((v,i)=><span key={i} className="vocab-chip">{v}</span>)}
                </div>
                {avail?(
                  <button className="launch-btn" onClick={()=>onStart(l.id)}>
                    {done?'Practice again →':'Start lesson →'}
                  </button>
                ):(
                  <p className="lesson-locked-hint">Complete {filtered[idx-1]?.title} to unlock</p>
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
  const [strengths,setStrengths]=useState(null);
  const [loading,setLoading]=useState(true);
  const [subtab,setSubtab]=useState('mistakes');
  useEffect(()=>{
    fetch(`${API}/heatmap/full`)
      .then(r=>r.json())
      .then(({heatmap,analysis,strengths})=>{setData(heatmap);setAnalysis(analysis);setStrengths(strengths);setLoading(false);});
  },[]);
  if(loading)return<div className="page-layout"><div className="empty-state">Analysing your progress…</div></div>;
  const maxCount=data?Math.max(...Object.values(data.error_map||{}).map(c=>c.count||0),1):1;
  const categories=data?.by_category||{};
  const catKeys=Object.keys(categories).sort((a,b)=>categories[b]-categories[a]);
  const maxCat=Math.max(...catKeys.map(k=>categories[k]),1);
  const fmtCat=k=>k.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  const CAT_TIPS={
    case_nominative:"Nominative case — the subject of a sentence.\ne.g. Hundurinn (the dog) er stór.",
    case_accusative:"Accusative case — the direct object.\ne.g. Ég sé hundinn (I see the dog).",
    case_dative:"Dative case — indirect object or with certain prepositions/verbs.\ne.g. Ég gef hundinum mat (I give the dog food).",
    case_genitive:"Genitive case — possession or 'of' relationships.\ne.g. Hundur Jóns (Jón's dog).",
    verb_conjugation:"Verb conjugation — matching the verb ending to person & number.\ne.g. ég er / þú ert / hann er.",
    verb_tense:"Verb tense — past, present, or future form.\ne.g. ég tala (I speak) vs. ég talaði (I spoke).",
    noun_gender:"Noun gender — Icelandic has masculine, feminine, neuter.\ne.g. hestur (m.), kona (f.), barn (n.).",
    adjective_agreement:"Adjective agreement — adjectives must match noun gender, number & case.\ne.g. stór hestur / stórt barn / stóra konu.",
    word_order:"Word order — Icelandic uses V2 (verb second) and inversion after fronting.\ne.g. Í gær fór ég (Yesterday I went, not *Í gær ég fór).",
    pronunciation:"Pronunciation — Icelandic-specific sounds like þ, ð, æ, ll, rl, double consonants.",
    vocabulary:"Vocabulary — using the correct word or choosing between similar words.",
    spelling:"Spelling — accent marks, double letters, and other orthographic rules.",
    other:"Other — errors that don't fall neatly into a specific grammar category.",
  };
  return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Performance Heatmap</h2><p className="page-sub">Your patterns across all sessions</p></div>
        <div className="level-pills">
          <button className={`pill ${subtab==='mistakes'?'active':''}`} onClick={()=>setSubtab('mistakes')}>Mistakes</button>
          <button className={`pill ${subtab==='strengths'?'active':''}`} onClick={()=>setSubtab('strengths')}>Strengths</button>
          <button className={`pill ${subtab==='analysis'?'active':''}`} onClick={()=>setSubtab('analysis')}>AI Analysis</button>
        </div>
      </div>
      {subtab==='mistakes'&&(
        <>
          <div className="hm-section">
            <p className="hm-section-title">Error Categories</p>
            {catKeys.length===0&&<div className="empty-state">No errors recorded yet. Start practicing!</div>}
            <div className="hm-categories">
              {catKeys.map(cat=>{
                const pct=Math.round((categories[cat]/maxCat)*100);
                const heat=pct>75?'heat-5':pct>50?'heat-4':pct>30?'heat-3':pct>15?'heat-2':'heat-1';
                return(
                  <div key={cat} className="hm-cat-row">
                    <span className="hm-cat-label">{fmtCat(cat)}</span>
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
      {subtab==='strengths'&&(
        <div className="strengths-panel">
          {!strengths&&<div className="empty-state">Loading strengths…</div>}
          {strengths&&strengths.total_errors===0&&(strengths.mastered_cards?.length||0)===0&&strengths.strong_categories?.length===0&&(
            <div className="empty-state">Keep practicing! Your strengths will appear here as data builds up.</div>
          )}
          {strengths&&(strengths.total_errors>0||(strengths.mastered_cards?.length||0)>0||strengths.strong_categories?.length>0)&&(
            <>
              {strengths.praise?.length>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Sigríður's Notes on Your Progress</p>
                  <div className="strengths-praise-list">
                    {strengths.praise.slice(0,8).map((p,i)=>(
                      <div key={i} className="strengths-praise-item">
                        <span className="strengths-praise-icon">✦</span>
                        <p className="strengths-praise-text">{p.text||p}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {strengths.strong_categories?.length>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Strong Categories</p>
                  <div className="strengths-chips">
                    {strengths.strong_categories.map((c,i)=>(
                      <span key={i} className="strengths-chip strengths-chip-good" title={CAT_TIPS[c]||fmtCat(c)}>{fmtCat(c)}</span>
                    ))}
                  </div>
                </div>
              )}
              {strengths.low_error_categories?.length>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Improving Categories</p>
                  <div className="strengths-chips">
                    {strengths.low_error_categories.map((c,i)=>(
                      <span key={i} className="strengths-chip strengths-chip-ok" title={CAT_TIPS[c.category||c]||fmtCat(c.category||c)}>{fmtCat(c.category||c)}</span>
                    ))}
                  </div>
                </div>
              )}
              {strengths.accuracy_trend?.length>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Error Rate Over Time <span className="hm-legend">(lower is better)</span></p>
                  <div className="strengths-trend">
                    {strengths.accuracy_trend.slice(-14).map((pt,i)=>{
                      const rate=pt.error_rate||0;
                      const barH=Math.round(rate*100);
                      const col=rate<0.1?'var(--green)':rate<0.25?'var(--gold)':'var(--red)';
                      return(
                        <div key={i} className="trend-col" title={`${pt.date}: ${Math.round(rate*100)}% error rate`}>
                          <div className="trend-bar-outer">
                            <div className="trend-bar-inner" style={{height:`${barH}%`,background:col}}/>
                          </div>
                          <span className="trend-label">{pt.date?.slice(5)}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              {(strengths.mastered_cards?.length||0)>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Mastered Vocabulary</p>
                  <div className="strengths-stat-row">
                    <div className="strengths-stat">
                      <span className="strengths-stat-num">{strengths.mastered_cards.length}</span>
                      <span className="strengths-stat-label">cards mastered</span>
                    </div>
                    <div className="strengths-stat">
                      <span className="strengths-stat-num">{strengths.total_errors}</span>
                      <span className="strengths-stat-label">errors logged (90 days)</span>
                    </div>
                  </div>
                </div>
              )}
              {strengths.weak_categories?.length>0&&(
                <div className="hm-section">
                  <p className="hm-section-title">Still Needs Work</p>
                  <div className="strengths-chips">
                    {strengths.weak_categories.map((c,i)=>(
                      <span key={i} className="strengths-chip strengths-chip-weak" title={CAT_TIPS[c.category||c]||fmtCat(c.category||c)}>{fmtCat(c.category||c)}</span>
                    ))}
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
function ProgressShell(){
  const [sub,setSub]=useState('progress');
  return(
    <div style={{display:'flex',flexDirection:'column',flex:1,overflow:'hidden'}}>
      <div className="fc-section-tabs">
        <button className={`fc-section-tab ${sub==='progress'?'active':''}`} onClick={()=>setSub('progress')}>Progress</button>
        <button className={`fc-section-tab ${sub==='heatmap'?'active':''}`}  onClick={()=>setSub('heatmap')}>Heatmap</button>
        <button className={`fc-section-tab ${sub==='cefr'?'active':''}`}     onClick={()=>setSub('cefr')}>CEFR</button>
      </div>
      {sub==='progress'&&<ProgressView/>}
      {sub==='heatmap'&&<HeatmapView/>}
      {sub==='cefr'&&<CefrView/>}
    </div>
  );
}

function ProgressView(){
  const [data,setData]=useState(null);const [days,setDays]=useState(30);const [loading,setLoading]=useState(true);
  useEffect(()=>{setLoading(true);fetch(`${API}/progress?days=${days}`).then(r=>r.json()).then(d=>{setData(d);setLoading(false);});},[days]);
  if(loading)return<div className="page-layout"><div className="empty-state">Loading…</div></div>;
  const totals=data?.totals||{};const daily=data?.daily||[];
  const streak=data?.streak||0;
  const maxTurns=Math.max(...daily.map(d=>d.turns||0),1);
  const accuracy=totals.total_turns>0?Math.round(((totals.total_turns-(totals.total_errors||0))/totals.total_turns)*100):null;
  return(
    <div className="page-layout">
      <div className="page-header">
        <h2 className="page-title">Your Progress</h2>
        <div className="days-toggle">{[7,30,90].map(n=><button key={n} className={`pill ${days===n?'active':''}`} onClick={()=>setDays(n)}>{n}d</button>)}</div>
      </div>

      <div className="progress-stat-group">
        <p className="progress-group-label">Last {days} days</p>
        <div className="stats-grid">
          {[
            {label:'Turns',value:totals.total_turns||0},
            {label:'Errors',value:totals.total_errors||0},
            {label:'Accuracy',value:accuracy!==null?`${accuracy}%`:'—'},
            {label:'Active Days',value:totals.active_days||0},
            {label:'Sessions',value:totals.total_sessions||0},
            {label:'Lessons Done',value:data?.lessons_completed||0},
          ].map((s,i)=>(
            <div key={i} className="stat-card">
              <div className="stat-value">{s.value}</div>
              <div className="stat-label">{s.label}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="progress-stat-group">
        <p className="progress-group-label">All time</p>
        <div className="stats-grid stats-grid-3">
          {[
            {label:'Day Streak',value:streak>0?`${streak} 🔥`:streak},
            {label:'Cards Total',value:data?.cards_total||0},
            {label:'Cards Due',value:data?.cards_due||0,highlight:(data?.cards_due||0)>0},
          ].map((s,i)=>(
            <div key={i} className={`stat-card ${s.highlight?'highlight':''}`}>
              <div className="stat-value">{s.value}</div>
              <div className="stat-label">{s.label}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="chart-section">
        <div className="chart-header">
          <p className="chart-title">Daily Practice</p>
          <div className="chart-legend">
            <span className="legend-dot legend-dot-turns"/>Turns
            <span className="legend-dot legend-dot-errors"/>Errors
          </div>
        </div>
        <div className="bar-chart">
          {daily.length===0&&<div className="empty-state">No data yet — start practicing!</div>}
          {daily.map((d,i)=>{
            const errFrac=d.turns>0?Math.min(d.errors_made/d.turns,1):0;
            return(
              <div key={i} className="bar-col">
                <div className="bar-wrap">
                  <div className="bar" style={{height:`${(d.turns/maxTurns)*100}%`}}
                       title={`${d.turns} turns · ${d.errors_made} errors`}>
                    <div className="bar-error-fill" style={{height:`${errFrac*100}%`}}/>
                  </div>
                </div>
                <span className="bar-label">{d.date?.slice(5)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// FLASHCARDS VIEW
// ═══════════════════════════════════════════════════════════════════════════════
const SENTENCE_CATS = ['phrase','sentence'];

function FlashcardsView(){
  const [section,setSection]=useState('vocabulary'); // 'vocabulary' | 'sentences' | 'visual'
  const [mode,setMode]=useState('browse');const [cards,setCards]=useState([]);const [dueCards,setDueCards]=useState([]);
  const [loading,setLoading]=useState(true);const [filter,setFilter]=useState('all');const [posFilter,setPosFilter]=useState('all');
  const [newIs,setNewIs]=useState('');const [newEn,setNewEn]=useState('');const [newNote,setNewNote]=useState('');const [newCat,setNewCat]=useState('vocabulary');const [newPos,setNewPos]=useState('');
  const [genTopic,setGenTopic]=useState('common greetings and everyday phrases');const [genCount,setGenCount]=useState(10);const [genLevel,setGenLevel]=useState('beginner');const [genLoading,setGenLoading]=useState(false);
  const [reviewIdx,setReviewIdx]=useState(0);const [showAns,setShowAns]=useState(false);const [revResult,setRevResult]=useState(null);
  // Visual-specific state
  const [visAnswer,setVisAnswer]=useState('');const [visAnswered,setVisAnswered]=useState(false);const [visResult,setVisResult]=useState(null);
  const [visGenProgress,setVisGenProgress]=useState(null); // {current, total}
  const [refreshingImgId,setRefreshingImgId]=useState(null);
  const visAnswerRef=useRef(null);
  const [fcRecording,setFcRecording]=useState(false);const [fcPronScore,setFcPronScore]=useState(null);const [fcScoring,setFcScoring]=useState(false);
  const fcMediaRecorder=useRef(null);const fcAudioChunks=useRef([]);
  const [quizCount,setQuizCount]=useState(10);const [quizQs,setQuizQs]=useState([]);const [quizIdx,setQuizIdx]=useState(0);
  const [quizSelected,setQuizSelected]=useState(null);const [quizAnswered,setQuizAnswered]=useState(false);
  const [quizLog,setQuizLog]=useState([]);const [quizState,setQuizState]=useState('start');const [quizLoading,setQuizLoading]=useState(false);
  const POS_LABELS=['noun','verb','adjective','adverb','preposition','conjunction','pronoun','phrase','other'];

  const loadCards=async()=>{
    setLoading(true);
    const[all,due]=await Promise.all([
      fetch(`${API}/flashcards?limit=500`).then(r=>r.json()),
      fetch(`${API}/flashcards?due_only=true`).then(r=>r.json()),
    ]);
    setCards(all);setDueCards(due);setLoading(false);
  };
  useEffect(()=>{loadCards();},[]);

  const isSentSec = section==='sentences';
  const isVisSec  = section==='visual';
  const sectionCards = isVisSec
    ? cards.filter(c=>c.category==='visual')
    : isSentSec
    ? cards.filter(c=>SENTENCE_CATS.includes(c.category))
    : cards.filter(c=>!SENTENCE_CATS.includes(c.category)&&c.category!=='visual');
  const sectionDueCards = isVisSec
    ? dueCards.filter(c=>c.category==='visual')
    : isSentSec
    ? dueCards.filter(c=>SENTENCE_CATS.includes(c.category))
    : dueCards.filter(c=>!SENTENCE_CATS.includes(c.category)&&c.category!=='visual');
  const filtered = (isSentSec||isVisSec)
    ? sectionCards
    : sectionCards.filter(c=>(filter==='all'||c.category===filter)&&(posFilter==='all'||c.part_of_speech===posFilter));
  const reviewCard=sectionDueCards[reviewIdx];

  const handleReview=async(correct)=>{
    const cardId=reviewCard.id;
    setRevResult(correct?'correct':'incorrect');
    await fetch(`${API}/flashcards/${cardId}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({card_id:cardId,correct})});
    // Remove the reviewed card from dueCards immediately so the badge reflects reality
    setDueCards(prev=>{
      const next=prev.filter(c=>c.id!==cardId);
      const sectionNext=isVisSec?next.filter(c=>c.category==='visual'):isSentSec?next.filter(c=>SENTENCE_CATS.includes(c.category)):next.filter(c=>!SENTENCE_CATS.includes(c.category)&&c.category!=='visual');
      setTimeout(()=>{
        setShowAns(false);setRevResult(null);setFcPronScore(null);
        setVisAnswer('');setVisAnswered(false);setVisResult(null);
        if(sectionNext.length===0){loadCards();setReviewIdx(0);setMode('browse');}
        else setReviewIdx(i=>Math.min(i,sectionNext.length-1));
      },700);
      return next;
    });
  };

  const handleAdd=async(e)=>{
    e.preventDefault();if(!newIs.trim()||!newEn.trim())return;
    const card=await fetch(`${API}/flashcards`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({icelandic:newIs,english:newEn,notes:newNote,category:newCat,part_of_speech:newPos})}).then(r=>r.json());
    setNewIs('');setNewEn('');setNewNote('');setNewCat(isVisSec?'visual':'vocabulary');setNewPos('');
    loadCards();setMode('browse');
    if(isVisSec&&card?.id){
      fetch(`${API}/flashcards/${card.id}/generate-image`,{method:'POST'}).then(r=>r.json())
        .then(d=>{const upd=c=>c.id===card.id?{...c,image_url:d.image_url}:c;
          setCards(p=>p.map(upd));setDueCards(p=>p.map(upd));}).catch(()=>{});
    }
  };

  const handleGenerate=async()=>{
    if(isVisSec){ await handleVisGenerate(); return; }
    setGenLoading(true);
    await fetch(`${API}/flashcards/generate`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count:genCount,level:genLevel,topic:genTopic,type:isSentSec?'sentence':'vocabulary'})});
    await loadCards();setGenLoading(false);setMode('browse');
  };

  const handleVisGenerate=async()=>{
    setGenLoading(true);
    try{
      const res=await fetch(`${API}/flashcards/generate`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({count:genCount,level:genLevel,topic:genTopic,type:'visual'})}).then(r=>r.json());
      const ids=res.ids||[];
      if(!ids.length){setGenLoading(false);return;}
      setVisGenProgress({current:0,total:ids.length});
      for(let i=0;i<ids.length;i++){
        try{await fetch(`${API}/flashcards/${ids[i]}/generate-image`,{method:'POST'});}
        catch(e){console.error(`Image gen failed for card ${ids[i]}:`,e);}
        setVisGenProgress({current:i+1,total:ids.length});
      }
      await loadCards();setVisGenProgress(null);setMode('browse');
    }catch(e){console.error(e);}
    setGenLoading(false);
  };

  const refreshImage=async(cardId)=>{
    setRefreshingImgId(cardId);
    try{
      const d=await fetch(`${API}/flashcards/${cardId}/generate-image?force=true`,{method:'POST'}).then(r=>r.json());
      const update=c=>c.id===cardId?{...c,image_url:d.image_url}:c;
      setCards(prev=>prev.map(update));setDueCards(prev=>prev.map(update));
    }catch(e){console.error(e);}
    setRefreshingImgId(null);
  };

  const checkVisAnswer=()=>{
    if(!visAnswer.trim()||!reviewCard)return;
    const given=visAnswer.trim().toLowerCase().normalize('NFC');
    const expected=reviewCard.icelandic.toLowerCase().normalize('NFC');
    const dist=levenshtein(given,expected);
    const correct=dist===0; const nearMiss=!correct&&dist<=1;
    setVisResult({correct:correct||nearMiss,nearMiss,expected:reviewCard.icelandic,given:visAnswer.trim()});
    setVisAnswered(true);
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

  const startQuiz=async()=>{
    setQuizLoading(true);
    try{
      const data=await fetch(`${API}/flashcards/quiz?count=${quizCount}`).then(r=>r.json());
      if(data.detail){alert(data.detail);setQuizLoading(false);return;}
      setQuizQs(data.questions);setQuizIdx(0);setQuizSelected(null);setQuizAnswered(false);setQuizLog([]);setQuizState('active');
    }catch(e){alert('Failed to load quiz.');}
    setQuizLoading(false);
  };

  const handleQuizSelect=(optIdx)=>{
    if(quizAnswered)return;
    setQuizSelected(optIdx);setQuizAnswered(true);
    const q=quizQs[quizIdx];
    setQuizLog(prev=>[...prev,{card_id:q.card_id,correct:optIdx===q.correct,q,chosen:optIdx}]);
  };

  const handleQuizNext=()=>{
    if(quizIdx+1>=quizQs.length){
      setQuizState('done');
      const answers=quizLog.map(l=>({card_id:l.card_id,correct:l.correct}));
      fetch(`${API}/flashcards/quiz/results`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers})}).catch(()=>{});
    }else{
      setQuizIdx(i=>i+1);setQuizSelected(null);setQuizAnswered(false);
    }
  };

  if(loading)return<div className="page-layout"><div className="empty-state">Loading…</div></div>;

  return(
    <div className="page-layout">
      <div className="page-header">
        <h2 className="page-title">Flashcards</h2>
        <div className="fc-header-actions">
          {section!=='reference'&&<span className="badge">{sectionDueCards.length} due</span>}
          {section!=='reference'&&<span className="badge badge-muted">{sectionCards.length} total</span>}
          {section!=='reference'&&<div className="level-pills">
            {((isSentSec||isVisSec)?['browse','review','add','generate']:['browse','review','quiz','add','generate']).map(m=>(
              <button key={m} className={`pill ${mode===m?'active':''}`} onClick={()=>{setMode(m);setReviewIdx(0);setShowAns(false);if(m==='quiz')setQuizState('start');}}>
                {m.charAt(0).toUpperCase()+m.slice(1)}
                {m==='review'&&sectionDueCards.length>0&&<span className="pill-badge">{sectionDueCards.length}</span>}
              </button>
            ))}
          </div>}
        </div>
      </div>

      <div className="fc-section-tabs">
        <button className={`fc-section-tab ${section==='vocabulary'?'active':''}`}
          onClick={()=>{setSection('vocabulary');setMode('browse');setReviewIdx(0);setShowAns(false);setFilter('all');setPosFilter('all');setNewCat('vocabulary');}}>
          Vocabulary
          <span className="fc-sec-count">{cards.filter(c=>!SENTENCE_CATS.includes(c.category)&&c.category!=='visual').length}</span>
        </button>
        <button className={`fc-section-tab ${section==='sentences'?'active':''}`}
          onClick={()=>{setSection('sentences');setMode('browse');setReviewIdx(0);setShowAns(false);setFilter('all');setNewCat('sentence');}}>
          Sentences
          <span className="fc-sec-count">{cards.filter(c=>SENTENCE_CATS.includes(c.category)).length}</span>
        </button>
        <button className={`fc-section-tab ${isVisSec?'active':''}`}
          onClick={()=>{setSection('visual');setMode('browse');setReviewIdx(0);setShowAns(false);setNewCat('visual');}}>
          Visual
          <span className="fc-sec-count">{cards.filter(c=>c.category==='visual').length}</span>
        </button>
        <button className={`fc-section-tab ${section==='reference'?'active':''}`}
          onClick={()=>setSection('reference')}>
          Reference
        </button>
      </div>

      {section==='reference' && <GrammarReferenceView/>}

      {section!=='reference' && mode==='review'&&(
        <div className="review-area">
          {sectionDueCards.length===0?(
            <div className="review-done">
              <div className="done-icon">✦</div><h3>All caught up!</h3>
              <p>No {isVisSec?'visual cards':isSentSec?'sentences':'cards'} due.</p>
              <button className="pill active" onClick={()=>setMode('browse')}>Browse</button>
            </div>
          ):isVisSec?(
            /* ── Visual card — image front, type answer ── */
            <div className="vis-review-card">
              <div className="fc-progress">{reviewIdx+1} / {sectionDueCards.length}</div>
              <div className="vis-img-wrap">
                {reviewCard?.image_url
                  ? <img className="vis-review-img" src={reviewCard.image_url} alt="Visual card"/>
                  : <div className="vis-img-placeholder"><span>No image</span></div>}
                <button className="vis-refresh-btn" title="Try a different image"
                  onClick={()=>refreshImage(reviewCard.id)} disabled={refreshingImgId===reviewCard?.id}>
                  {refreshingImgId===reviewCard?.id?'…':'🔄'}
                </button>
              </div>
              {!visAnswered?(
                <>
                  <p className="vis-prompt">What is this in Icelandic?</p>
                  <div className="vis-answer-row">
                    <input ref={visAnswerRef} className="vis-answer-input" value={visAnswer}
                      onChange={e=>setVisAnswer(e.target.value)}
                      onKeyDown={e=>e.key==='Enter'&&checkVisAnswer()}
                      placeholder="Type the Icelandic word…" autoComplete="off" autoCorrect="off" spellCheck="false"/>
                    <button className="vis-check-btn" onClick={checkVisAnswer} disabled={!visAnswer.trim()}>Check</button>
                  </div>
                </>
              ):(
                <>
                  <div className={`vis-result ${visResult?.correct?'vis-result-correct':'vis-result-wrong'}`}>
                    <span className="vis-result-icon">{visResult?.correct?'✓':'✗'}</span>
                    <div>
                      {visResult?.nearMiss&&<p className="vis-result-nearmiss">Close — watch the spelling!</p>}
                      {!visResult?.correct&&<p>Correct: <strong className="icelandic-inline">{visResult?.expected}</strong></p>}
                      <p className="vis-result-english">{reviewCard?.english}</p>
                      {reviewCard?.notes&&<p className="vis-result-note">{reviewCard.notes}</p>}
                    </div>
                    <button className="vis-tts-btn" onClick={()=>playWord(reviewCard?.icelandic)} title="Hear it"><SpeakerIcon/></button>
                  </div>
                  <div className="fc-actions">
                    <button className="fc-btn fc-wrong" onClick={()=>handleReview(false)}><span>✗</span>Again</button>
                    <button className="fc-btn fc-correct" onClick={()=>handleReview(true)}><span>✓</span>Got it</button>
                  </div>
                </>
              )}
            </div>
          ):isSentSec?(
            /* ── Sentence flip card — English front, Icelandic back ── */
            <div className={`flashcard ${showAns?'flipped':''} ${revResult||''}`} onClick={()=>{if(!showAns)setShowAns(true);}}>
              <div className="fc-progress">{reviewIdx+1} / {sectionDueCards.length}</div>
              <div className="fc-front">
                <p className="fc-prompt-label">How do you say this in Icelandic?</p>
                <p className="fc-word">{reviewCard?.english}</p>
                <button className="fc-reveal-btn" onClick={e=>{e.stopPropagation();setShowAns(true);}}>Reveal Icelandic</button>
              </div>
              {showAns&&(
                <div className="fc-back">
                  <div className="fc-word-row">
                    <p className="fc-word icelandic">{reviewCard?.icelandic}</p>
                    <button className="fc-play-btn" onClick={e=>{e.stopPropagation();playWord(reviewCard?.icelandic);}} title="Listen"><SpeakerIcon/></button>
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
          ):(
            /* ── Vocabulary flip card — Icelandic front, English back ── */
            <div className={`flashcard ${showAns?'flipped':''} ${revResult||''}`} onClick={()=>{if(!showAns)setShowAns(true);}}>
              <div className="fc-progress">{reviewIdx+1} / {sectionDueCards.length}</div>
              <div className="fc-front">
                <span className="fc-category">{reviewCard?.category}</span>
                {reviewCard?.part_of_speech&&<span className="fc-pos">{reviewCard.part_of_speech}</span>}
                <div className="fc-word-row">
                  <p className="fc-word icelandic">{reviewCard?.icelandic}</p>
                  <button className="fc-play-btn" onClick={e=>{e.stopPropagation();playWord(reviewCard?.icelandic);}} title="Listen">
                    <SpeakerIcon/>
                  </button>
                </div>
                <div className="fc-pron-row">
                  <button
                    className={`fc-mic-btn ${fcRecording?'recording':''}`}
                    onClick={e=>e.stopPropagation()}
                    onMouseDown={e=>{e.preventDefault();e.stopPropagation();if(!fcRecording)startFcRecording();}}
                    onMouseUp={e=>{e.preventDefault();e.stopPropagation();if(fcRecording)stopFcRecording(reviewCard?.icelandic);}}
                    onTouchStart={e=>{e.preventDefault();e.stopPropagation();if(!fcRecording)startFcRecording();}}
                    onTouchEnd={e=>{e.preventDefault();e.stopPropagation();if(fcRecording)stopFcRecording(reviewCard?.icelandic);}}
                    title={fcRecording?'Release to score':'Hold to speak'}
                  >
                    {fcRecording?<MicActiveIcon/>:<MicIcon/>}
                    <span>{fcRecording?'Release…':'Say it'}</span>
                  </button>
                  {fcScoring&&<span className="fc-scoring">Scoring…</span>}
                </div>
                {fcPronScore&&<PronunciationPanel score={fcPronScore}/>}
                <button className="fc-reveal-btn" onClick={e=>{e.stopPropagation();setShowAns(true);}}>Reveal answer</button>
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

      {section!=='reference' && mode==='quiz'&&(
        <div className="quiz-area">
          {quizState==='start'&&(
            <div className="quiz-start">
              <div className="quiz-start-icon">?</div>
              <h3 className="quiz-start-title">Vocabulary Quiz</h3>
              <p className="quiz-start-desc">Multiple-choice questions drawn from your flashcard deck. Half will ask you to translate English → Icelandic, half Icelandic → English.</p>
              <div className="quiz-count-row">
                <span className="quiz-count-label">Questions:</span>
                {[5,10,20].map(n=>(
                  <button key={n} className={`pill ${quizCount===n?'active':''}`} onClick={()=>setQuizCount(n)}>{n}</button>
                ))}
              </div>
              {cards.length<4&&<p className="quiz-warn">You need at least 4 flashcards to take a quiz. Generate or add some first.</p>}
              <button className="pill active quiz-start-btn" onClick={startQuiz} disabled={quizLoading||cards.length<4}>
                {quizLoading?'Loading…':'Start Quiz'}
              </button>
            </div>
          )}
          {quizState==='active'&&quizQs.length>0&&(()=>{
            const q=quizQs[quizIdx];
            const letters=['A','B','C','D'];
            return(
              <div className="quiz-q-card">
                <div className="quiz-progress-row">
                  <div className="quiz-progress-bar">
                    <div className="quiz-progress-fill" style={{width:`${((quizIdx)/quizQs.length)*100}%`}}/>
                  </div>
                  <span className="quiz-progress-label">{quizIdx+1} / {quizQs.length}</span>
                </div>
                <span className="quiz-direction-badge">{q.direction==='en_to_is'?'EN → IS':'IS → EN'}</span>
                <p className="quiz-question">{q.question}</p>
                <div className="quiz-options">
                  {q.options.map((opt,i)=>{
                    let cls='quiz-option';
                    if(quizAnswered){
                      if(i===q.correct)cls+=' quiz-opt-correct';
                      else if(i===quizSelected)cls+=' quiz-opt-wrong';
                    }else if(i===quizSelected)cls+=' quiz-opt-selected';
                    return(
                      <button key={i} className={cls} onClick={()=>handleQuizSelect(i)} disabled={quizAnswered}>
                        <span className="quiz-opt-letter">{letters[i]}</span>
                        <span className="quiz-opt-text">{opt}</span>
                        {quizAnswered&&i===q.correct&&<span className="quiz-opt-tick">✓</span>}
                        {quizAnswered&&i===quizSelected&&i!==q.correct&&<span className="quiz-opt-tick">✗</span>}
                      </button>
                    );
                  })}
                </div>
                {quizAnswered&&q.notes&&<p className="quiz-note">{q.notes}</p>}
                {quizAnswered&&(
                  <button className="pill active quiz-next-btn" onClick={handleQuizNext}>
                    {quizIdx+1>=quizQs.length?'See Results':'Next →'}
                  </button>
                )}
              </div>
            );
          })()}
          {quizState==='done'&&(()=>{
            const score=quizLog.filter(l=>l.correct).length;
            const pct=Math.round((score/quizLog.length)*100);
            return(
              <div className="quiz-results">
                <div className="quiz-score-circle">
                  <span className="quiz-score-num">{pct}%</span>
                  <span className="quiz-score-sub">{score}/{quizLog.length}</span>
                </div>
                <p className="quiz-score-msg">{pct>=80?'Excellent work!':pct>=60?'Good effort — keep practicing!':'Keep at it — review your cards and try again.'}</p>
                <div className="quiz-breakdown">
                  {quizLog.map((l,i)=>(
                    <div key={i} className={`quiz-br-item ${l.correct?'br-correct':'br-wrong'}`}>
                      <span className="quiz-br-tick">{l.correct?'✓':'✗'}</span>
                      <div className="quiz-br-body">
                        <p className="quiz-br-q">{l.q.question}</p>
                        {!l.correct&&<p className="quiz-br-ans">Correct: <strong>{l.q.options[l.q.correct]}</strong> · You chose: <em>{l.q.options[l.chosen]}</em></p>}
                      </div>
                    </div>
                  ))}
                </div>
                <div className="quiz-done-actions">
                  <button className="pill" onClick={()=>{setQuizState('start');}}>Quiz Again</button>
                  <button className="pill" onClick={()=>setMode('browse')}>Browse Cards</button>
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {section!=='reference' && mode==='add'&&(
        <form className="add-card-form" onSubmit={handleAdd}>
          <h3 className="form-title">Add a {isVisSec?'visual card':isSentSec?'sentence':'flashcard'}</h3>
          {isVisSec&&<p className="vis-gen-note">An image will be generated automatically after saving.</p>}
          <div className="form-group"><label>Icelandic {isVisSec?'word':isSentSec?'sentence':''}</label><input value={newIs} onChange={e=>setNewIs(e.target.value)} placeholder={isVisSec?'e.g. hestur':isSentSec?'e.g. Hvernig hefur þú það í dag?':'e.g. Góðan daginn'} required/></div>
          <div className="form-group"><label>English {isVisSec?'word':isSentSec?'equivalent':''}</label><input value={newEn} onChange={e=>setNewEn(e.target.value)} placeholder={isVisSec?'e.g. horse':isSentSec?'e.g. How are you today?':'e.g. Good morning'} required/></div>
          <div className="form-group"><label>Notes</label><input value={newNote} onChange={e=>setNewNote(e.target.value)} placeholder={isVisSec?'e.g. masculine noun (m.)':isSentSec?'Grammar note (optional)':'Grammar note or example'}/></div>
          {!isSentSec&&!isVisSec&&(
            <>
              <div className="form-group"><label>Category</label>
                <div className="level-pills">{['vocabulary','grammar','phrase'].map(c=><button type="button" key={c} className={`pill ${newCat===c?'active':''}`} onClick={()=>setNewCat(c)}>{c}</button>)}</div>
              </div>
              <div className="form-group"><label>Part of speech</label>
                <div className="level-pills">{POS_LABELS.map(p=><button type="button" key={p} className={`pill ${newPos===p?'active':''}`} onClick={()=>setNewPos(newPos===p?'':p)}>{p}</button>)}</div>
              </div>
            </>
          )}
          <div className="form-actions">
            <button type="button" className="pill" onClick={()=>setMode('browse')}>Cancel</button>
            <button type="submit" className="pill active">Save</button>
          </div>
        </form>
      )}

      {section!=='reference' && mode==='generate'&&(
        <div className="add-card-form">
          <h3 className="form-title">Generate {isVisSec?'visual cards':isSentSec?'sentences':'cards'} with AI</h3>
          {isVisSec&&visGenProgress&&(
            <div className="vis-gen-progress">
              <div className="vis-gen-bar"><div className="vis-gen-fill" style={{width:`${Math.round(visGenProgress.current/visGenProgress.total*100)}%`}}/></div>
              <p className="vis-gen-label">Generating images… {visGenProgress.current} / {visGenProgress.total}</p>
            </div>
          )}
          <div className="form-group">
            <label>{isVisSec?'Topic (concrete nouns only)':isSentSec?'Situation / topic':'Topic'}</label>
            <input value={genTopic} onChange={e=>setGenTopic(e.target.value)}
              placeholder={isVisSec?'e.g. kitchen items, animals, transport':isSentSec?'e.g. ordering at a café, asking for directions':'e.g. common greetings and everyday phrases'}/>
          </div>
          <div className="form-group"><label>Count</label><input type="number" min="5" max="30" value={genCount} onChange={e=>setGenCount(parseInt(e.target.value))}/></div>
          <div className="form-group"><label>Level</label>
            <div className="level-pills">{LEVELS.map(l=><button type="button" key={l} className={`pill ${genLevel===l?'active':''}`} onClick={()=>setGenLevel(l)}>{l}</button>)}</div>
          </div>
          {isVisSec&&<p className="vis-gen-note">Images are generated after the word list. This may take a minute if the SD server is busy.</p>}
          <div className="form-actions">
            <button className="pill" onClick={()=>setMode('browse')}>Cancel</button>
            <button className="pill active" onClick={handleGenerate} disabled={genLoading||!!visGenProgress}>
              {genLoading||(visGenProgress&&visGenProgress.current<visGenProgress.total)?'Generating…':`Generate ${genCount} ${isVisSec?'visual cards':isSentSec?'sentences':'cards'}`}
            </button>
          </div>
        </div>
      )}

      {section!=='reference' && mode==='browse'&&(
        <>
          {!isSentSec&&(
            <>
              <div className="filter-row">
                {['all','vocabulary','grammar','phrase'].map(f=>(
                  <button key={f} className={`pill ${filter===f?'active':''}`} onClick={()=>setFilter(f)}>
                    {f.charAt(0).toUpperCase()+f.slice(1)}
                    <span className="pill-count">{f==='all'?sectionCards.length:sectionCards.filter(c=>c.category===f).length}</span>
                  </button>
                ))}
              </div>
              <div className="filter-row">
                {['all',...POS_LABELS].map(p=>{
                  const cnt=p==='all'?sectionCards.length:sectionCards.filter(c=>c.part_of_speech===p).length;
                  if(p!=='all'&&cnt===0)return null;
                  return(
                    <button key={p} className={`pill ${posFilter===p?'active':''}`} onClick={()=>setPosFilter(p)}>
                      {p.charAt(0).toUpperCase()+p.slice(1)}
                      <span className="pill-count">{cnt}</span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
          {filtered.length===0&&<div className="empty-state">No {isVisSec?'visual cards':isSentSec?'sentences':'cards'} yet — try Generate!</div>}
          {isVisSec&&filtered.length>0&&(
            <div className="vis-cards-grid">
              {filtered.map(card=>(
                <div key={card.id} className="vis-card-item">
                  <div className="vis-card-img-wrap">
                    {card.image_url
                      ? <img className="vis-card-img" src={card.image_url} alt={card.english} loading="lazy"/>
                      : <div className="vis-img-placeholder"><span>No image</span></div>}
                    <button className="vis-card-refresh" onClick={()=>refreshImage(card.id)}
                      disabled={refreshingImgId===card.id} title="Generate new image">
                      {refreshingImgId===card.id?'…':'🔄'}
                    </button>
                  </div>
                  <div className="vis-card-meta">
                    <div className="card-is-row">
                      <p className="vis-card-is icelandic">{card.icelandic}</p>
                      <button className="card-play-btn" onClick={()=>playWord(card.icelandic)} title="Pronounce"><SpeakerIcon/></button>
                    </div>
                    <p className="vis-card-en">{card.english}</p>
                    {card.notes&&<p className="card-note">{card.notes}</p>}
                    <div className="card-stats">
                      <span>{card.times_seen} seen</span><span>{card.times_correct} correct</span>
                    </div>
                  </div>
                  <button className="delete-btn" onClick={()=>handleDelete(card.id)}><TrashIcon/></button>
                </div>
              ))}
            </div>
          )}
          <div className={(!isVisSec&&isSentSec)?'sentence-cards-list':(!isVisSec&&!isSentSec)?'cards-grid':''}
               style={isVisSec?{display:'none'}:{}}>
            {filtered.map(card=>(
              <div key={card.id} className={isSentSec?'sentence-card-item':'card-item'}>
                {isSentSec?(
                  <>
                    <div className="sentence-card-top">
                      <div className="sentence-card-tts">
                        <button className="card-play-btn" onClick={()=>playWord(card.icelandic)} title="Listen"><SpeakerIcon/></button>
                      </div>
                      <button className="delete-btn" onClick={()=>handleDelete(card.id)}><TrashIcon/></button>
                    </div>
                    <p className="sentence-card-is icelandic">{card.icelandic}</p>
                    <p className="sentence-card-en">{card.english}</p>
                    {card.notes&&<p className="card-note">{card.notes}</p>}
                  </>
                ):(
                  <>
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
                  </>
                )}
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
// ═══════════════════════════════════════════════════════════════════════════════
// DRILL VIEW
// ═══════════════════════════════════════════════════════════════════════════════

const DRILL_CATEGORIES = [
  {id:'case_nominative',    label:'Nominative',  desc:'The subject of a sentence — the one doing the action. e.g. Hundurinn bítur (The dog bites) — hundurinn is nominative.'},
  {id:'case_accusative',    label:'Accusative',  desc:'The direct object — the one receiving the action. e.g. Ég sé hundinn (I see the dog) — hundinn is accusative.'},
  {id:'case_dative',        label:'Dative',      desc:'Indirect objects and many prepositions (í, á, með, frá, hjá). e.g. Ég gef hundinum mat (I give the dog food) — hundinum is dative.'},
  {id:'case_genitive',      label:'Genitive',    desc:'Possession or "of" relationships. e.g. Bíll Jóns (Jón\'s car) — Jóns is genitive.'},
  {id:'verb_conjugation',   label:'Conjugation', desc:'Matching verb endings to subject pronoun in present tense. e.g. ég tala, þú talar, hann talar, við tölum.'},
  {id:'verb_tense',         label:'Verb Tense',  desc:'Converting present tense to simple past (þátíð). Weak verbs add -ði/-ti; strong verbs change the stem vowel. e.g. tala → talaði, fara → fór.'},
  {id:'noun_gender',        label:'Noun Gender', desc:'Every Icelandic noun is masculine, feminine, or neuter — this determines all case endings and adjective agreement. e.g. hestur (m.), kona (f.), barn (n.).'},
  {id:'adjective_agreement',label:'Adjectives',  desc:'Adjective endings must match the noun\'s gender, case, and definiteness. e.g. stór hestur (big horse, m. nom.) vs. stórt barn (big child, n. nom.).'},
];

function DrillView(){
  const [category, setCategory]   = useState('case_accusative');
  const [level,    setLevel]      = useState('beginner');
  const [phase,    setPhase]      = useState('idle'); // idle|loading|question|feedback
  const [questions,setQuestions]  = useState([]);
  const [qIdx,     setQIdx]       = useState(0);
  const [input,    setInput]      = useState('');
  const [result,   setResult]     = useState(null); // {correct, near_miss, expected, explanation}
  const [session,  setSession]    = useState({correct:0, total:0});
  const [stats,    setStats]      = useState(null);
  const inputRef = useRef(null);

  const loadStats = async () => {
    try{const d=await fetch(`${API}/drill/stats`).then(r=>r.json()); setStats(d);}
    catch(e){console.error(e);}
  };

  useEffect(()=>{loadStats();},[]);

  const fetchBatch = async (cat, lvl) => {
    setPhase('loading');
    try{
      const d = await fetch(`${API}/drill/questions?category=${cat}&level=${lvl}&count=10`).then(r=>r.json());
      if(!d.questions||d.questions.length===0){setPhase('idle');return;}
      setQuestions(d.questions);
      setQIdx(0);
      setInput('');
      setResult(null);
      setSession({correct:0,total:0});
      setPhase('question');
      setTimeout(()=>inputRef.current?.focus(),50);
    }catch(e){console.error(e);setPhase('idle');}
  };

  const focusWeak = async () => {
    let freshStats;
    try{ freshStats = await fetch(`${API}/drill/stats`).then(r=>r.json()); setStats(freshStats); }
    catch(e){ console.error(e); return; }
    const cats = Object.entries(freshStats?.by_category||{});
    if(cats.length===0){ fetchBatch(category, level); return; }
    cats.sort((a,b)=>(a[1].accuracy??100)-(b[1].accuracy??100));
    const weakest = cats[0][0];
    setCategory(weakest);
    fetchBatch(weakest, level);
  };

  const checkAnswer = async () => {
    if(phase!=='question'||!input.trim()) return;
    const q = questions[qIdx];
    const r = await fetch(`${API}/drill/answer`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        category: q.category||category, difficulty: level,
        question: q.question, expected: q.expected,
        answer_variants: q.answer_variants||[], given: input.trim(),
        explanation: q.explanation||'',
      }),
    }).then(r=>r.json());
    setResult(r);
    setSession(s=>({correct: s.correct+(r.correct?1:0), total: s.total+1}));
    setPhase('feedback');
    loadStats();
  };

  const nextQuestion = () => {
    const nextIdx = qIdx+1;
    if(nextIdx>=questions.length){setPhase('idle');return;}
    setQIdx(nextIdx);
    setInput('');
    setResult(null);
    setPhase('question');
    setTimeout(()=>inputRef.current?.focus(),50);
  };

  const handleKey = (e) => {
    if(e.key==='Enter' && phase==='question') checkAnswer();
    if(e.key==='Enter' && phase==='feedback') nextQuestion();
  };

  const q = questions[qIdx];
  const fmtCat = id => DRILL_CATEGORIES.find(c=>c.id===id)?.label || id.replace(/_/g,' ');

  return(
    <div className="page-layout">
      <div className="page-header">
        <div>
          <h2 className="page-title">Grammar Drill</h2>
          <p className="page-sub">Targeted morphology practice</p>
        </div>
        <button className="pill active" onClick={focusWeak} title="Switch to your weakest category">Focus weak area</button>
      </div>

      {/* Controls */}
      <div className="drill-controls">
        <div className="drill-control-group">
          <span className="drill-label">Category</span>
          <div className="level-pills">
            {DRILL_CATEGORIES.map(c=>(
              <button key={c.id}
                className={`pill ${category===c.id?'active':''}`}
                onClick={()=>setCategory(c.id)}
                disabled={phase==='loading'}
                title={c.desc}
              >{c.label}</button>
            ))}
          </div>
        </div>
        <div className="drill-control-group">
          <span className="drill-label">Level</span>
          <div className="level-pills">
            {['beginner','intermediate','advanced'].map(l=>(
              <button key={l} className={`pill ${level===l?'active':''}`}
                onClick={()=>setLevel(l)} disabled={phase==='loading'}>
                {l.charAt(0).toUpperCase()+l.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Start / idle */}
      {phase==='idle'&&(
        <div className="drill-card drill-start">
          <p className="drill-start-hint">
            {session.total>0
              ? `Session complete — ${session.correct}/${session.total} correct (${Math.round(session.correct/session.total*100)}%)`
              : 'Select a category and level, then start drilling.'}
          </p>
          <button className="drill-start-btn" onClick={()=>fetchBatch(category,level)}>
            {session.total>0 ? 'New Batch' : 'Start Drill'}
          </button>
        </div>
      )}

      {phase==='loading'&&(
        <div className="drill-card drill-start">
          <div className="empty-state">Generating questions…</div>
        </div>
      )}

      {/* Active question */}
      {(phase==='question'||phase==='feedback')&&q&&(
        <div className="drill-card">
          <div className="drill-progress">
            <span className="drill-progress-label">{qIdx+1} / {questions.length}</span>
            <span className="drill-session-score">{session.correct} correct</span>
          </div>

          <p className="drill-category-badge">{fmtCat(q.category||category)}</p>
          <p className="drill-question">{q.question}</p>
          {q.base_form&&<p className="drill-base">Base form: <em className="icelandic-inline">{q.base_form}</em></p>}

          <div className="drill-input-row">
            <input
              ref={inputRef}
              className={`drill-input ${phase==='feedback'?(result?.correct?'drill-input-correct':'drill-input-wrong'):''}`}
              type="text" value={input}
              onChange={e=>setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder="Type your answer…"
              disabled={phase==='feedback'}
              autoComplete="off" autoCorrect="off" spellCheck="false"
            />
            {phase==='question'&&(
              <button className="drill-check-btn" onClick={checkAnswer} disabled={!input.trim()}>Check</button>
            )}
            {phase==='feedback'&&(
              <button className="drill-next-btn" onClick={nextQuestion}>
                {qIdx+1>=questions.length ? 'Done' : 'Next →'}
              </button>
            )}
          </div>

          {phase==='feedback'&&result&&(
            <div className={`drill-feedback ${result.correct?'drill-feedback-correct':'drill-feedback-wrong'}`}>
              <span className="drill-feedback-icon">{result.correct?'✓':'✗'}</span>
              <div>
                {result.correct&&result.near_miss&&(
                  <p className="drill-feedback-nearmiss">Close — watch your spelling.</p>
                )}
                {!result.correct&&(
                  <p className="drill-feedback-expected">Correct answer: <strong className="icelandic-inline">{result.expected}</strong></p>
                )}
                {result.explanation&&<p className="drill-feedback-explain">{result.explanation}</p>}
              </div>
            </div>
          )}
        </div>
      )}

      {/* All-time stats */}
      {stats&&Object.keys(stats.by_category||{}).length>0&&(
        <div className="hm-section">
          <p className="hm-section-title">All-time accuracy by category</p>
          <div className="hm-categories">
            {Object.entries(stats.by_category)
              .sort((a,b)=>a[1].accuracy-b[1].accuracy)
              .map(([cat,d])=>{
                const pct=d.accuracy;
                const col=pct>=80?'drill-acc-high':pct>=50?'drill-acc-mid':'drill-acc-low';
                return(
                  <div key={cat} className="hm-cat-row">
                    <span className="hm-cat-label">{fmtCat(cat)}</span>
                    <div className="hm-bar-outer">
                      <div className={`hm-bar-inner ${col}`} style={{width:`${pct}%`}}/>
                    </div>
                    <span className="hm-cat-count">{pct}% <span className="drill-attempts">({d.attempts})</span></span>
                  </div>
                );
              })}
          </div>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// LIBRARY VIEW
// ═══════════════════════════════════════════════════════════════════════════════
function LibraryView(){
  const [mode,setMode]               = useState('books'); // books | reader | search
  const [books,setBooks]             = useState([]);
  const [booksLoading,setBooksLoading] = useState(true);
  const [activeBook,setActiveBook]   = useState(null);
  const [currentPage,setCurrentPage] = useState(1);
  const [completedPages,setCompletedPages] = useState(new Set());
  const [pageInput,setPageInput]     = useState('1');
  const [imgLoading,setImgLoading]   = useState(true);
  const [zoom,setZoom]               = useState(1.0);
  const [searchQuery,setSearchQuery] = useState('');
  const [searchResults,setSearchResults] = useState([]);
  const [searching,setSearching]     = useState(false);
  const [searchDone,setSearchDone]   = useState(false);

  useEffect(()=>{
    Promise.all([
      fetch('/rag/books').then(r=>r.json()),
      fetch(`${API}/library/progress`).then(r=>r.json()).catch(()=>({})),
    ]).then(([booksData, progressData])=>{
      const books=(booksData.books||[]).map(b=>({
        ...b, completedCount: progressData[b.filename]||0,
      }));
      setBooks(books); setBooksLoading(false);
    }).catch(()=>setBooksLoading(false));
  },[]);

  // Keyboard navigation in reader
  useEffect(()=>{
    if(mode!=='reader'||!activeBook) return;
    const handler=(e)=>{
      if(e.key==='ArrowLeft')  goToPage(currentPage-1);
      if(e.key==='ArrowRight') goToPage(currentPage+1);
    };
    window.addEventListener('keydown',handler);
    return()=>window.removeEventListener('keydown',handler);
  },[mode,activeBook,currentPage]);

  const openBook = async(book)=>{
    setActiveBook(book); setImgLoading(true); setZoom(1.0); setMode('reader');
    try{
      const d=await fetch(`${API}/library/progress/${book.filename}`).then(r=>r.json());
      const completed=new Set(d.completed_pages);
      setCompletedPages(completed);
      // Resume from first incomplete page; fall back to page 1 if all done
      let resume=1;
      for(let p=1;p<=book.page_count;p++){ if(!completed.has(p)){resume=p;break;} }
      setCurrentPage(resume); setPageInput(String(resume));
    }catch(e){ setCompletedPages(new Set()); setCurrentPage(1); setPageInput('1'); }
  };

  const goToPage=(n)=>{
    if(!activeBook) return;
    const p=Math.max(1,Math.min(activeBook.page_count,n));
    setCurrentPage(p); setPageInput(String(p)); setImgLoading(true);
  };

  const toggleComplete=async()=>{
    if(!activeBook) return;
    const done=completedPages.has(currentPage);
    await fetch(`${API}/library/progress`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:activeBook.filename,page_num:currentPage,completed:!done})});
    setCompletedPages(prev=>{ const n=new Set(prev); done?n.delete(currentPage):n.add(currentPage); return n; });
    if(!done && currentPage < activeBook.page_count) goToPage(currentPage+1);
  };

  const doSearch=async(e)=>{
    if(e) e.preventDefault();
    if(!searchQuery.trim()) return;
    setSearching(true); setSearchDone(false);
    try{
      const d=await fetch('/rag/query',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({query:searchQuery,top_k:8})}).then(r=>r.json());
      setSearchResults(d.chunks||[]); setMode('search'); setSearchDone(true);
    }catch(e){ console.error(e); }
    setSearching(false);
  };

  const openResult=(chunk)=>{
    const book=books.find(b=>b.source===chunk.source);
    if(!book||!chunk.page_number) return;
    setActiveBook(book); setCurrentPage(chunk.page_number);
    setPageInput(String(chunk.page_number)); setImgLoading(true); setMode('reader');
    fetch(`${API}/library/progress/${book.filename}`).then(r=>r.json())
      .then(d=>setCompletedPages(new Set(d.completed_pages))).catch(()=>{});
  };

  const bookTitle={
    'complete_icelandic':'Complete Icelandic',
    'colloquial-icelandic-the-complete-course-for-beginners':'Colloquial Icelandic',
  };

  // ── Books grid ──────────────────────────────────────────────────────────────
  if(mode==='books') return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Library</h2><p className="page-sub">Browse your Icelandic textbooks and track reading progress</p></div>
      </div>
      <form className="lib-search-bar" onSubmit={doSearch}>
        <input className="lib-search-input" value={searchQuery} onChange={e=>setSearchQuery(e.target.value)}
          placeholder="Find content about… e.g. ordering at a café, verb conjugation, greetings"/>
        <button className="lib-search-btn" type="submit" disabled={searching||!searchQuery.trim()}>
          {searching?'Searching…':'Search'}
        </button>
      </form>
      {booksLoading&&<div className="empty-state">Loading books…</div>}
      {!booksLoading&&books.length===0&&<div className="empty-state">No PDFs found. Add books to the rag-service/pdfs directory.</div>}
      <div className="lib-books-grid">
        {books.map(b=>{
          const pagesRead=b.completedCount||0;
          const pct=b.page_count>0?Math.round((pagesRead/b.page_count)*100):0;
          return(
            <div key={b.filename} className="lib-book-card" onClick={()=>openBook(b)}>
              <div className="lib-book-cover">
                <img src={`/rag/pdfs/${b.filename}/page/1`} alt={b.title} loading="lazy"/>
              </div>
              <div className="lib-book-meta">
                <p className="lib-book-title">{b.title}</p>
                <p className="lib-book-pages">{b.page_count} pages</p>
                <div className="lib-book-progress-bar">
                  <div className="lib-book-progress-fill" style={{width:`${pct}%`}}/>
                </div>
                <p className="lib-book-pct">{pagesRead} / {b.page_count} pages read</p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );

  // ── Search results ──────────────────────────────────────────────────────────
  if(mode==='search') return(
    <div className="page-layout">
      <div className="page-header">
        <button className="lib-back-btn" onClick={()=>setMode('books')}>← Library</button>
        <div><h2 className="page-title">Search Results</h2></div>
      </div>
      <form className="lib-search-bar" onSubmit={doSearch}>
        <input className="lib-search-input" value={searchQuery} onChange={e=>setSearchQuery(e.target.value)}
          placeholder="Search the books…"/>
        <button className="lib-search-btn" type="submit" disabled={searching||!searchQuery.trim()}>
          {searching?'Searching…':'Search'}
        </button>
      </form>
      {searchDone&&searchResults.length===0&&<div className="empty-state">No results found.</div>}
      <div className="lib-results">
        {searchResults.map((r,i)=>(
          <div key={i} className="lib-result">
            <div className="lib-result-header">
              <span className="lib-result-source">{bookTitle[r.source]||r.source}</span>
              {r.page_number&&<span className="lib-result-page">p. {r.page_number}</span>}
              <span className="lib-result-score">{Math.round(r.relevance*100)}% match</span>
            </div>
            <p className="lib-result-text">{r.text.slice(0,280)}{r.text.length>280?'…':''}</p>
            {r.page_number&&books.find(b=>b.source===r.source)&&(
              <button className="lib-result-open" onClick={()=>openResult(r)}>Open page {r.page_number} →</button>
            )}
          </div>
        ))}
      </div>
    </div>
  );

  // ── Reader ──────────────────────────────────────────────────────────────────
  if(mode==='reader'&&activeBook){
    const isDone=completedPages.has(currentPage);
    const pct=activeBook.page_count>0?Math.round((completedPages.size/activeBook.page_count)*100):0;
    return(
      <div className="page-layout">

        {/* Header: topbar + search */}
        <div className="lib-reader-header">
          <div className="lib-reader-topbar">
            <button className="lib-back-btn" onClick={()=>{
            setMode('books');
            fetch(`${API}/library/progress`).then(r=>r.json()).then(p=>{
              setBooks(prev=>prev.map(b=>({...b,completedCount:p[b.filename]||0})));
            }).catch(()=>{});
          }}>← Library</button>
            <div className="lib-reader-title">
              <span className="lib-reader-book-name">{activeBook.title}</span>
              <span className="lib-reader-progress">{completedPages.size}/{activeBook.page_count} pages · {pct}%</span>
            </div>
            <button className={`lib-complete-btn${isDone?' done':''}`} onClick={toggleComplete}>
              {isDone?'✓ Complete':'Mark complete'}
            </button>
          </div>
          <form className="lib-search-bar" onSubmit={doSearch}>
            <input className="lib-search-input" value={searchQuery} onChange={e=>setSearchQuery(e.target.value)}
              placeholder="Search books for a topic… e.g. ordering at a café"/>
            <button className="lib-search-btn" type="submit" disabled={searching||!searchQuery.trim()}>
              {searching?'Searching…':'Search books'}
            </button>
          </form>
        </div>

        <div className="lib-reader-progress-bar">
          <div className="lib-reader-progress-fill" style={{width:`${pct}%`}}/>
        </div>

        {/* Fixed viewport-centered arrows — scoped to .main via backdrop-filter */}
        <button className="lib-page-arrow lib-page-arrow-left"
          onClick={()=>goToPage(currentPage-1)} disabled={currentPage<=1}>‹</button>
        <button className="lib-page-arrow lib-page-arrow-right"
          onClick={()=>goToPage(currentPage+1)} disabled={currentPage>=activeBook.page_count}>›</button>

        <div className="lib-page-wrap">
          {imgLoading&&<div className="lib-page-skeleton"/>}
          <img
            key={`${activeBook.filename}-${currentPage}`}
            className="lib-page-img"
            style={{display:imgLoading?'none':'block', width:`${zoom*100}%`, maxWidth:`${zoom*700}px`}}
            src={`/rag/pdfs/${activeBook.filename}/page/${currentPage}`}
            alt={`Page ${currentPage}`}
            onLoad={()=>setImgLoading(false)}
            onError={()=>setImgLoading(false)}
          />
          {isDone&&!imgLoading&&<div className="lib-page-done-badge">✓</div>}
        </div>

        {/* Nav: page counter + zoom */}
        <div className="lib-nav-row">
          <button className="lib-nav-btn" onClick={()=>goToPage(currentPage-1)} disabled={currentPage<=1}>←</button>
          <div className="lib-page-input-wrap">
            <input className="lib-page-input" type="number" min="1" max={activeBook.page_count}
              value={pageInput}
              onChange={e=>setPageInput(e.target.value)}
              onBlur={()=>goToPage(parseInt(pageInput)||1)}
              onKeyDown={e=>e.key==='Enter'&&goToPage(parseInt(pageInput)||1)}/>
            <span className="lib-page-total">/ {activeBook.page_count}</span>
          </div>
          <button className="lib-nav-btn" onClick={()=>goToPage(currentPage+1)} disabled={currentPage>=activeBook.page_count}>→</button>
          <div className="lib-zoom-controls">
            <button className="lib-zoom-btn" onClick={()=>setZoom(z=>Math.max(0.5,+(z-0.25).toFixed(2)))} disabled={zoom<=0.5} title="Zoom out">−</button>
            <span className="lib-zoom-label">{Math.round(zoom*100)}%</span>
            <button className="lib-zoom-btn" onClick={()=>setZoom(z=>Math.min(3,+(z+0.25).toFixed(2)))} disabled={zoom>=3} title="Zoom in">+</button>
          </div>
        </div>

      </div>
    );
  }

  return null;
}

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
// ═══════════════════════════════════════════════════════════════════════════════
// PRONUNCIATION GUIDE
// ═══════════════════════════════════════════════════════════════════════════════
const PRON_DATA = {
  vowels: {
    title: 'Vowels',
    rows: [
      ['Á á', 'ow in "cow"',    ['ár (year)',       'ást (love)',       'lát (manner)'],       'á = open back, not "ah"'],
      ['É é', 'ye in "yes"',   ['él (sleet)',      'éta (eat)',        'vél (machine)'],      'starts with a y-glide'],
      ['Í í', 'ee in "feet"',  ['ís (ice)',        'líf (life)',       'tími (time)'],        'same as English "ee", held longer'],
      ['Ó ó', 'oh — rounded',  ['ós (estuary)',    'fótur (foot)',     'stór (big)'],         'rounder/tenser than English "oh"'],
      ['Ú ú', 'oo in "food"',  ['úr (drizzle)',    'hús (house)',      'búa (live)'],         'pure vowel, no glide'],
      ['Ý ý', 'ee in "feet"',  ['ýr (yew)',        'dýr (animal)',     'ýta (push)'],         'identical to Í'],
      ['Æ æ', 'eye',           ['læra (learn)',    'mæta (meet)',      'ræða (speech)'],      'pure diphthong, one syllable'],
      ['Ö ö', 'u in "burn"',   ['önd (duck)',      'öxl (shoulder)',   'þröng (narrow)'],     'rounded front vowel; purse lips and say "e"'],
    ],
  },
  consonants: {
    title: 'Special Consonants',
    rows: [
      ['Þ þ', 'th in "think"',   ['þing (parliament)', 'þakk (thanks)',    'þrír (three)'],     'voiceless — tongue between teeth, no buzz'],
      ['Ð ð', 'th in "this"',    ['að (to)',            'bað (bath)',       'maður (man)'],      'voiced — tongue between teeth, with buzz'],
      ['G g', 'y before e/i',    ['gegn (against)',     'gefa (give)',      'gildi (value)'],    'soft g (like "y") before e, i, j'],
      ['J j', 'y in "yes"',      ['já (yes)',           'jörð (earth)',     'jakki (jacket)'],   'always a "y" sound, never like English j'],
      ['R r', 'rolled/trilled',  ['rauður (red)',       'rigning (rain)',   'rós (rose)'],       'tip of tongue taps the ridge behind top teeth'],
      ['S s', 'always sharp s',  ['sama (same)',        'saga (saga)',      'sól (sun)'],        'never buzzes like z'],
      ['X x', 'ks',              ['sex (six)',          'lax (salmon)',     'buxur (trousers)'], 'always "ks", never "gz"'],
    ],
  },
  clusters: {
    title: 'Consonant Clusters',
    rows: [
      ['LL',    'voiceless lateral — "tl" with a hiss', ['fjall (mountain)',  'öll (all)',         'hjalli (ledge)'],    'unique to Icelandic/Welsh; tongue sides, air rushes past'],
      ['RL',    'like "rdl"',                           ['karl (man)',        'harla (very)',      'ferli (process)'],   'r colours the l into a retroflex'],
      ['RN',    'like "rdn"',                           ['barn (child)',      'þorn (thorn)',      'horn (corner)'],     'r colours the n; slightly nasal'],
      ['HV',    'kv',                                   ['hvað (what)',       'hvar (where)',      'hvenær (when)'],     'written hv, always said "kv" in modern Icelandic'],
      ['GJ',    'y in "yes"',                           ['gjöf (gift)',       'gjósa (gush)',      'gjald (fee)'],       'the g is silent; only the j/y sound remains'],
      ['KJ',    'ch — soft palate',                     ['kjöt (meat)',       'kjósa (choose)',    'kjöll (keel)'],      'like "ch" in German "ich"'],
      ['FN/FJ', 'bn / bv',                              ['fnykur (stench)',   'fjall (mountain)',  'fjörður (fjord)'],   'f becomes voiced b before n or j'],
      ['NG',    'ng-g (both sounded)',                  ['ungur (young)',     'enginn (nobody)',   'langur (long)'],     'unlike English "sing" — both n and g are audible'],
      ['NN',    'nasalised / long n',                   ['kanna (jug)',       'hann (he)',         'vinna (work)'],      'held longer than a single n'],
    ],
  },
  aspiration: {
    title: 'Pre-aspiration',
    desc: 'Icelandic double stops (pp, tt, kk) have a noticeable breath (h-sound) BEFORE the stop, not after. This is the opposite of English.',
    rows: [
      ['pp', 'h+p — "ahp"', ['uppá (upon)',       'appelsína (orange)', 'knappur (button)'],  'breathe out before the p'],
      ['tt', 'h+t — "aht"', ['köttur (cat)',      'nótt (night)',       'máttugur (mighty)'], 'the double-t sounds like "ht"'],
      ['kk', 'h+k — "ahk"', ['ekki (not)',        'bekkur (bench)',     'bakki (bank)'],      'breathe out before the k'],
      ['bb', 'pre-voiced',  ['ebba (ebb tide)',   'gabba (deceive)',    'rabbi (rabbi)'],     'voiced counterpart, softer'],
    ],
  },
  stress: {
    title: 'Stress & Rhythm',
    rows: [
      ['Stress',       'Always on the first syllable',                   ['Ísland (Iceland)',  'kennari (teacher)', 'stúdent (student)'], 'no exceptions in native words'],
      ['Vowel length', 'Long before one consonant, short before two',   ['fara (go)',         'barn (child)',      'vera (be)'],         'fara: long á; barn: short a before rn'],
      ['Intonation',   'Relatively flat, falling at end of sentence',   ['takk (thanks)',     'gott (good)',       'já (yes)'],          'avoid rising intonation on statements'],
    ],
  },
};

const GRAMMAR_REF_DATA = {
  pronouns: {
    title: 'Pronouns',
    type: 'pronouns',
    rows: [
      // [english, nominative, accusative, dative, genitive]
      ['I',         'ég',   'mig',   'mér',   'mín'],
      ['you (sg)',  'þú',   'þig',   'þér',   'þín'],
      ['he',        'hann', 'hann',  'honum', 'hans'],
      ['she',       'hún',  'hana',  'henni', 'hennar'],
      ['it',        'það',  'það',   'því',   'þess'],
      ['we',        'við',  'okkur', 'okkur', 'okkar'],
      ['you (pl)',  'þið',  'ykkur', 'ykkur', 'ykkar'],
      ['they (m)',  'þeir', 'þá',    'þeim',  'þeirra'],
      ['they (f)',  'þær',  'þær',   'þeim',  'þeirra'],
      ['they (n)',  'þau',  'þau',   'þeim',  'þeirra'],
    ],
  },
  prepositions: {
    title: 'Prepositions',
    type: 'words',
    headers: ['Prep', 'English', 'Example', 'Case'],
    rows: [
      ['í',    'in / into',    'í bæinn (into town) · í bænum (in town)',           'acc → motion · dat → location'],
      ['á',    'on / onto',    'á borðið (onto table) · á borðinu (on table)',       'acc → motion · dat → location'],
      ['til',  'to / of',      'til Reykjavíkur (to Reykjavík)',                    'genitive always'],
      ['frá',  'from',         'frá honum (from him) · frá Íslandi (from Iceland)',  'dative always'],
      ['með',  'with',         'með mér (with me) · með þér (with you)',            'dative always'],
      ['af',   'off / from',   'af hverju (why) · af borðinu (off the table)',      'dative always'],
      ['um',   'about / around','um hann (about him) · um daginn (during the day)', 'accusative always'],
      ['fyrir','for / before', 'fyrir mig (for me) · fyrir viku (a week ago)',      'accusative always'],
      ['eftir','after',        'eftir mat (after food) · eftir þig (after you)',    'accusative always'],
      ['við',  'at / by',      'við hlið (beside) · við hann (against him)',        'accusative always'],
    ],
  },
  conjunctions: {
    title: 'Conjunctions',
    type: 'words',
    headers: ['Word', 'English', 'Example', 'Note'],
    rows: [
      ['og',           'and',       'ég og þú (you and I)',                                    '—'],
      ['en',           'but',       'stór en feginn (big but happy)',                          'also "and" in formal writing'],
      ['eða',          'or',        'kaffi eða te? (coffee or tea?)',                          '—'],
      ['ef',           'if',        'ef þú vilt (if you want)',                                'introduces conditional clauses'],
      ['þegar',        'when',      'þegar hann kom (when he arrived)',                        'not hvenær (at what time?)'],
      ['þó',           'although',  'þó hún sé þreytt (though she is tired)',                 'often with subjunctive mood'],
      ['vegna þess að','because',   'vegna þess að ég vil (because I want)',                  'most common causal conjunction'],
      ['en samt',      'but still', 'hann er þreyttur en samt labbar (tired yet still walks)', 'very common in speech'],
    ],
  },
  adverbs: {
    title: 'Adverbs',
    type: 'words',
    headers: ['Word', 'English', 'Example', 'Type'],
    rows: [
      ['núna',    'now',         'Ég er núna heima (I am home now)',                   'time'],
      ['þá',      'then',        'Þá fór hann (Then he went)',                         'time'],
      ['strax',   'right away',  'Komdu strax! (Come right away!)',                    'time'],
      ['alltaf',  'always',      'Hún er alltaf glaður (She is always happy)',         'frequency'],
      ['oft',     'often',       'Við förum oft út (We often go out)',                 'frequency'],
      ['stundum', 'sometimes',   'Stundum er það kalt (Sometimes it is cold)',         'frequency'],
      ['aldrei',  'never',       'Hann sefur aldrei snemma (He never sleeps early)',   'frequency'],
      ['hér',     'here',        'Hann er hér (He is here)',                           'place'],
      ['þar',     'there',       'Hún er þar (She is there)',                          'place'],
      ['burt',    'away',        'Farðu burt! (Go away!)',                             'place'],
      ['vel',     'well',        'Hann talar vel íslensku (He speaks Icelandic well)', 'manner'],
      ['saman',   'together',    'Við förum saman (We go together)',                   'manner'],
    ],
  },
};

function PronunciationView(){
  const sections = ['vowels','consonants','clusters','aspiration','stress'];
  const [active, setActive] = useState('vowels');
  const sec = PRON_DATA[active];
  return(
    <div className="page-layout">
      <div className="page-header">
        <div><h2 className="page-title">Pronunciation Guide</h2><p className="page-sub">Icelandic sounds that don't exist in English</p></div>
      </div>
      <div className="pron-nav">
        {sections.map(s=>(
          <button key={s} className={`pill ${active===s?'active':''}`} onClick={()=>setActive(s)}>
            {PRON_DATA[s].title}
          </button>
        ))}
      </div>
      {sec.desc&&<p className="pron-section-desc">{sec.desc}</p>}
      <div className="pron-table-wrap">
        <table className="pron-table">
          <thead>
            <tr>
              <th>Letter / Pattern</th>
              <th>Sounds Like</th>
              <th>Examples</th>
              <th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {sec.rows.map(([letter,sounds,example,note],i)=>(
              <tr key={i}>
                <td>
                  {letter.includes(' ') ? (
                    <span className="pron-letter-pair">
                      <span className="pron-letter">{letter.split(' ')[0]}</span>
                      <span className="pron-letter-lower">{letter.split(' ')[1]}</span>
                    </span>
                  ) : (
                    <span className="pron-letter">{letter}</span>
                  )}
                </td>
                <td className="pron-sounds">{sounds}</td>
                <td>
                  <div className="pron-examples">
                    {(Array.isArray(example)?example:[example]).map((ex,j)=>{
                      const pi=ex.indexOf(' (');
                      const word=pi>=0?ex.slice(0,pi):ex;
                      const trans=pi>=0?ex.slice(pi+2,-1):'';
                      return(
                        <div key={j} className="pron-example-row">
                          <button className="pron-play-btn" onClick={()=>playWord(word)} title={`Hear "${word}"`}><SpeakerIcon/></button>
                          <span className="pron-example icelandic">{word}</span>
                          {trans&&<span className="pron-example-en">({trans})</span>}
                        </div>
                      );
                    })}
                  </div>
                </td>
                <td className="pron-note">{note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pron-tip-box">
        <span className="pron-tip-icon">✦</span>
        <p className="pron-tip-text">
          {active==='vowels'&&'Icelandic vowels are pure — they don\'t glide into another sound the way English vowels do. Á (like "cow") and Æ ("eye") are the most common surprises.'}
          {active==='consonants'&&'Þ and Ð look exotic but you already know both sounds from English "think" and "this". The rolled R and soft G are the trickiest to acquire.'}
          {active==='clusters'&&'LL is the most distinctively Icelandic sound — practice "at-l" with the air escaping around your tongue rather than over it. HV→KV is a consistent rule with no exceptions.'}
          {active==='aspiration'&&'Pre-aspiration is what makes Icelandic sound so distinctive. In English we breathe OUT after stops; in Icelandic the breath comes BEFORE. Listen for it in "köttur" and "ekki".'}
          {active==='stress'&&'First-syllable stress is absolute in Icelandic — even foreign loanwords get shifted. This gives the language its characteristic rhythm and is easy to learn as a rule.'}
        </p>
      </div>
    </div>
  );
}

function GrammarReferenceView(){
  const sections = ['pronouns','prepositions','conjunctions','adverbs'];
  const [active, setActive] = useState('pronouns');
  const sec = GRAMMAR_REF_DATA[active];
  const tips = {
    pronouns:     'Pronouns decline through four cases. Nominative is the subject (ég — I). Accusative is the direct object (mig — me). Dative is the indirect object (mér — to me). Genitive shows possession (mín — mine).',
    prepositions: 'Every preposition governs a specific case — this must be memorised per word. í and á each take accusative for motion ("into/onto") and dative for location ("in/on"). Most others always take the same case.',
    conjunctions: 'Conjunctions don\'t change form in Icelandic. Þegar (when) is the most commonly confused: it means "at the time that", not "at what time?" — that\'s hvenær.',
    adverbs:      'Adverbs never decline in Icelandic. Frequency adverbs (alltaf, oft, stundum, aldrei) typically sit just after the verb: "Hún er alltaf glaður".',
  };
  return(
    <div>
      <div className="pron-nav" style={{marginBottom:'1rem'}}>
        {sections.map(s=>(
          <button key={s} className={`pill ${active===s?'active':''}`} onClick={()=>setActive(s)}>
            {GRAMMAR_REF_DATA[s].title}
          </button>
        ))}
      </div>

      {sec.type==='pronouns' ? (
        <div className="pron-table-wrap">
          <table className="pron-table pronoun-table">
            <thead>
              <tr>
                <th>English</th>
                <th>Nominative<br/><span className="case-hint">subject</span></th>
                <th>Accusative<br/><span className="case-hint">direct obj</span></th>
                <th>Dative<br/><span className="case-hint">indirect obj</span></th>
                <th>Genitive<br/><span className="case-hint">possession</span></th>
              </tr>
            </thead>
            <tbody>
              {sec.rows.map(([eng,nom,acc,dat,gen],i)=>(
                <tr key={i}>
                  <td className="pron-sounds">{eng}</td>
                  {[nom,acc,dat,gen].map((form,j)=>(
                    <td key={j}>
                      <span className="pron-case-cell">
                        <button className="pron-play-btn" onClick={()=>playWord(form)} title={`Hear "${form}"`}><SpeakerIcon/></button>
                        <span className="pron-example icelandic">{form}</span>
                      </span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="pron-table-wrap">
          <table className="pron-table">
            <thead>
              <tr>{sec.headers.map(h=><th key={h}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {sec.rows.map(([word,english,example,note],i)=>(
                <tr key={i}>
                  <td>
                    <span className="pron-word-cell">
                      <button className="pron-play-btn" onClick={()=>playWord(word)} title={`Hear "${word}"`}><SpeakerIcon/></button>
                      <span className="pron-example icelandic">{word}</span>
                    </span>
                  </td>
                  <td className="pron-sounds">{english}</td>
                  <td className="pron-note">{example}</td>
                  <td><span className={`type-tag type-tag-${note.replace(/\s.*/,'')}`}>{note}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="pron-tip-box" style={{marginTop:'1rem'}}>
        <span className="pron-tip-icon">✦</span>
        <p className="pron-tip-text">{tips[active]}</p>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAP VIEW
// ═══════════════════════════════════════════════════════════════════════════════
const MAPTILER_KEY   = import.meta.env.VITE_MAPTILER_KEY || '';
const MAPTILER_STYLE = `https://api.maptiler.com/maps/openstreetmap/style.json?key=${MAPTILER_KEY}`;

function MapView(){
  const containerRef = useRef(null);
  const mapRef       = useRef(null);
  const [selected,   setSelected]   = useState(null); // {name, nameEn, layerType}
  const [addedNames, setAddedNames] = useState(new Set());

  useEffect(()=>{
    if(!containerRef.current||mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAPTILER_STYLE,
      center: [-18.5, 65.0],
      zoom: 6,
      attributionControl: {compact:true},
    });
    mapRef.current = map;

    map.on('click', e=>{
      const features = map.queryRenderedFeatures(e.point);
      const hit = features.find(f=>f.properties?.name);
      if(!hit){ setSelected(null); return; }
      const name    = hit.properties.name;
      const nameEn  = hit.properties['name:en'] || hit.properties.name_en || null;
      const layerType = (hit.layer?.id||'').replace(/_/g,' ');
      playWord(name);
      setSelected({name, nameEn, layerType});
    });

    map.on('mousemove', e=>{
      const features = map.queryRenderedFeatures(e.point);
      map.getCanvas().style.cursor = features.some(f=>f.properties?.name) ? 'pointer' : '';
    });

    return ()=>{ map.remove(); mapRef.current=null; };
  },[]);

  const addFlashcard = async()=>{
    if(!selected||addedNames.has(selected.name)) return;
    try{
      await fetch(`${API}/flashcards`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          icelandic: selected.name,
          english:   selected.nameEn||selected.name,
          notes:     selected.layerType?`Feature type: ${selected.layerType}`:'Icelandic place name',
          category:  'vocabulary',
          part_of_speech: 'proper noun',
        })});
      setAddedNames(p=>new Set([...p,selected.name]));
    }catch(e){console.error(e);}
  };

  const chatAbout = ()=>{
    if(!selected) return;
    const prompt = selected.nameEn
      ? `Tell me about ${selected.name} (${selected.nameEn}) in Iceland. What does the name mean and what should I know about it?`
      : `Tell me about the place called "${selected.name}" in Iceland. What does the name mean?`;
    goToTab('chat');
    setTimeout(()=>seedChatInput(prompt),150);
  };

  return(
    <div className="map-view">
      <div className="map-header">
        <h2 className="page-title">Iceland Map</h2>
        <p className="map-hint">Click any place name on the map to hear it pronounced</p>
      </div>
      <div className="map-wrap" ref={containerRef}/>
      {selected&&(
        <div className="map-info-bar">
          <div className="map-info-left">
            <button className="map-speak-btn" onClick={()=>playWord(selected.name)} title="Pronounce again">
              <SpeakerIcon/>
            </button>
            <div>
              <div className="map-info-name">{selected.name}</div>
              {selected.nameEn&&<div className="map-info-en">{selected.nameEn}</div>}
              {selected.layerType&&<div className="map-info-type">{selected.layerType}</div>}
            </div>
          </div>
          <div className="map-info-actions">
            <button
              className={`map-action-btn${addedNames.has(selected.name)?' map-action-added':''}`}
              onClick={addFlashcard}
              disabled={addedNames.has(selected.name)}
            >
              {addedNames.has(selected.name)?'Added ✓':'+ Flashcard'}
            </button>
            <button className="map-action-btn map-chat-btn" onClick={chatAbout}>
              Chat about it
            </button>
            <button className="map-info-close" onClick={()=>setSelected(null)} title="Dismiss">×</button>
          </div>
        </div>
      )}
    </div>
  );
}

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
const PronIcon    =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8"/></svg>;
const DrillIcon   =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>;
const HistoryIcon =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="16" height="16"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/></svg>;
const LibraryIcon =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="18" height="18"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="12" y1="6" x2="16" y2="6"/><line x1="12" y1="10" x2="16" y2="10"/></svg>;
const TrashIcon   =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>;
const MapIcon     =()=><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" width="18" height="18"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/></svg>;
