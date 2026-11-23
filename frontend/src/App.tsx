import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { Activity, ArrowDownToLine, AudioLines, Captions, Check, ChevronDown, CircleHelp, Clock3, Copy, FileVideo, Film, FolderKanban, FolderInput, Globe2, Languages, LayoutDashboard, ListVideo, LoaderCircle, Lock, LockOpen, Mic, MoreHorizontal, Pencil, Play, Plus, Search, Settings2, ShieldCheck, Sparkles, Trash2, Upload, WandSparkles, X } from 'lucide-react'
import SubtitleTimeline, {type Waveform} from './SubtitleTimeline'
import './style.css'
import Register from './Register'
import Login, {type Account} from './Login'
import AdminPanel from './AdminPanel'
import RevisionExport from './RevisionExport'
import LivePanel from './LivePanel'
import GlossaryPanel from './GlossaryPanel'
import {dialectLabels, JobSteps, type JobStatus} from './JobDetails'
import {bilingualCues, exportVideoRequest, startVideoDownload, type VideoCue} from './videoDownload'

const API = `${window.location.protocol}//${window.location.hostname}:8001/api/v1`
type Project = {id:string;name:string;description?:string|null;video_id?:string|null;video_title?:string|null;duration_ms?:number|null;job_id?:string|null;progress?:number;source_url?:string|null;source_type:string;status:string;created_at:string;segment_count:number;media_ready?:boolean;error_message?:string;source_language?:string;target_language?:string;track_id?:string;target_track_id?:string;execution_environment?:string}
type Video = {id:string;title:string;source_type:string;status:string}
type Segment = {id:string;sequence_number:number;start_time:number;end_time:number;source_text:string;translated_text:string;translated_id?:string|null;translated_locked?:boolean;translated_start_ms?:number|null;translated_end_ms?:number|null;confidence:number|null;speaker_label?:string;dialect_label?:string|null;dialect_confidence?:number|null;words?:{word_text:string;start_ms:number;end_ms:number}[]}
type VideoTrack = {track_id:string;language:string;track_type:'ORIGINAL'|'TRANSLATION';source_track_id:string|null;imported:boolean}
type Language = 'vi'|'en'
type Track = 'original'|'translation'|'bilingual'
type Issue = {segment_id:string;issue_type:string}
type JobHistory = {job_id:string;job_type:string;status:string;progress:number;error_message?:string;title?:string}
const statuses: Record<string, string> = {ready:'Sẵn sàng',pending:'Đang chờ xử lý',running:'Đang nhận dạng',completed:'Hoàn tất',failed:'Thất bại',cancelled:'Đã hủy'}
type Page = 'overview'|'projects'|'editor'|'live'|'history'|'settings'|'admin'
const time = (v:number) => `${Math.floor(v/60).toString().padStart(2,'0')}:${(v%60).toFixed(2).padStart(5,'0')}`
const issueHints:Record<string,string>={
 CPS:'Đoạn đọc quá nhanh. Rút gọn câu hoặc kéo dài thời gian hiển thị.',
 CPL:'Dòng quá dài. Ngắt dòng hoặc tách đoạn.',
 DURATION_SHORT:'Thời gian hiển thị ngắn. Kiểm tra mốc kết thúc.',
 DURATION_LONG:'Thời gian hiển thị dài. Kiểm tra mốc hoặc tách đoạn.',
 OVERLAP:'Thời gian chồng với đoạn khác. Điều chỉnh mốc.',
 DUPLICATE:'Nội dung có thể trùng. Kiểm tra đoạn kế bên.',
 LOW_CONFIDENCE:'Độ tin cậy thấp. Nghe lại lời thoại để đối chiếu.',
 HALLUCINATION_REPEAT:'Nội dung lặp mô hình có thể đang ảo giác. Kiểm tra hoặc cắt đoạn này.',
}
const projectKey = (p:Project) => p.video_id ? `${p.id}/${p.video_id}` : p.id
function projectUrl(p:Project, suffix='', parameters:Record<string,string>={}){
 const query=new URLSearchParams(parameters)
 if(p.video_id)query.set('video_id', p.video_id)
 return `/projects/${p.id}${suffix}${query.size?'?'+query.toString():''}`
}

export default function App(){
 const [page,setPage]=useState<Page>('overview'),[projects,setProjects]=useState<Project[]>([]),[active,setActive]=useState<Project|null>(null),[segments,setSegments]=useState<Segment[]>([]),[loading,setLoading]=useState(false),[notice,setNotice]=useState(''),[query,setQuery]=useState(''),[url,setUrl]=useState(''),[projectName,setProjectName]=useState(''),[showCreate,setShowCreate]=useState(false),[saving,setSaving]=useState(false),[liveText,setLiveText]=useState(''),[liveFinal,setLiveFinal]=useState<string[]>([]),[listening,setListening]=useState(false),[health,setHealth]=useState(false)
 const fileRef=useRef<HTMLInputElement>(null)
 const projectFileRef=useRef<HTMLInputElement>(null)
 const playerRef=useRef<HTMLVideoElement>(null)
 const [playhead,setPlayhead]=useState(0)
 const [duration,setDuration]=useState(0)
 const overviewUrlRef=useRef<HTMLInputElement>(null)
 const [creating,setCreating]=useState(false)
 const [createError,setCreateError]=useState('')
 const [mysql,setMysql]=useState(false)
 const [showRegister,setShowRegister]=useState(false)
 const [showLogin,setShowLogin]=useState(false)
 const [demoLanguage,setDemoLanguage]=useState('en-US')
 const [account,setAccount]=useState<Account|null>(null)
 const [trackVersion,setTrackVersion]=useState<number|null>(null)
 const [showRevisions,setShowRevisions]=useState(false)
 const [exportingVideo,setExportingVideo]=useState(false)
 const [videoDownload,setVideoDownload]=useState<{key:string;url:string}|null>(null)
 const [showGlossary,setShowGlossary]=useState(false)
 const [contextAvailable,setContextAvailable]=useState(false)
 const [speechRouting,setSpeechRouting]=useState('')
 const [dialectAvailable,setDialectAvailable]=useState(false)
 const [translationMode,setTranslationMode]=useState<'local'|'beam'|'context'>('beam')
 const [issues,setIssues]=useState<Issue[]>([])
 const [changingTrack,setChangingTrack]=useState(false)
 const [historyId,setHistoryId]=useState('')
 const [history,setHistory]=useState<JobHistory[]>([])
 const [historyJob,setHistoryJob]=useState<string>('')
 const [job,setJob]=useState<JobStatus|null>(null)
 useEffect(()=>{
  if(!notice)return
  const timer=window.setTimeout(()=>setNotice(''),5000)
  return ()=>window.clearTimeout(timer)
 },[notice])
 const [selectedProjects,setSelectedProjects]=useState<string[]>([])
 const [videos,setVideos]=useState<Video[]>([])
 const [subtitleTracks,setSubtitleTracks]=useState<VideoTrack[]>([])
 const [targetSegments,setTargetSegments]=useState<Segment[]>([])
 const [waveform,setWaveform]=useState<Waveform|null>(null)
 const [skipAsr,setSkipAsr]=useState(false)
 const [showImport,setShowImport]=useState(false)
 const [showSource,setShowSource]=useState(false)
 const [source,setSource]=useState<Language|'auto'>('auto')
 const [translationLanguage,setTranslationLanguage]=useState<Language>('en')
 const [accurate,setAccurate]=useState(true)
 const [captionTrack,setCaptionTrack]=useState<Track>('original')
 const [translating,setTranslating]=useState(false)
 const activeId=useRef<string|null>(null)
 const editorRequest=useRef(0)
 const frame=useRef(0)
 async function request(path:string,options?:RequestInit){const res=await fetch(`${API}${path}`,{credentials:'include',...options});if(!res.ok){const body=await res.json().catch(()=>({}));if(res.status===401){setShowLogin(true);setAccount(null)}throw new Error(typeof body.detail==='string'?body.detail:'Dữ liệu hoặc thao tác không hợp lệ.')}return res}
 async function loadProjects(){try{const r=await fetch(`${API}/projects`,{credentials:'include'});if(r.status===401){setProjects([]);setHealth(true);return []}if(!r.ok)throw new Error('Không tải được danh sách dự án.');const data:Project[]=await r.json();setProjects(data);setHealth(true);return data}catch(e){setHealth(false);setNotice((e as Error).message);return []}}
 useEffect(()=>{
  loadProjects()
  request('/capabilities').then(r=>r.json()).then(data=>{
   setMysql(data.database==='mysql')
   setContextAvailable(Boolean(data.context_translation))
   if(data.context_translation)setTranslationMode('context')
   setSpeechRouting(data.speech_to_text)
   setDialectAvailable(Boolean(data.local_dialect))
  }).catch(()=>{})
  const controller=new AbortController()
  fetch(`${API}/auth/me`,{credentials:'include',signal:controller.signal})
   .then(response=>response.ok?response.json():null)
   .then(account=>{if(!controller.signal.aborted)setAccount(account)})
   .catch(()=>{})
  return ()=>controller.abort()
 },[])
 async function signOut(){
  try{
   const response=await fetch(`${API}/auth/logout`,{method:'POST',credentials:'include'})
   if(!response.ok)throw new Error('Không thể đăng xuất. Vui lòng thử lại.')
   setAccount(null)
   if(mysql){
    activeId.current=null
    setActive(null)
    setSegments([])
    setProjects([])
    setHistoryId('')
    setPage('overview')
   }
   setNotice('Đã đăng xuất.')
  }catch(error){setNotice((error as Error).message)}
 }
 async function openProject(p:Project,requestedTrack?:string,requestedTarget?:string){
  const key=projectKey(p)
  const changed=activeId.current!==key
  activeId.current=key
  const revision=++editorRequest.current
  if(changed){setSegments([]);setIssues([]);setTrackVersion(null);setShowSource(false);setJob(null);setSubtitleTracks([]);setTargetSegments([])}
  setActive(p)
  setPage('editor')
  setLoading(true)
  try{
   const preview:{tracks:VideoTrack[]}|null=mysql&&p.video_id?await request(`/videos/${p.video_id}/preview?include_segments=false`).then(r=>r.json()):null
   const available=preview?.tracks??[]
   const selected=available.find(track=>track.track_id===(requestedTrack??p.track_id))
   const target=available.find(track=>track.source_track_id===selected?.track_id&&(
    !requestedTarget||track.track_id===requestedTarget||track.language===requestedTarget))
   if(selected)p={...p,track_id:selected.track_id,source_language:selected.language,target_track_id:target?.track_id,
    target_language:target?.language}
   const base=projectUrl(p,'/segments')
   const params=new URLSearchParams()
   if(p.track_id&&mysql)params.set('track_id',p.track_id)
   if(p.target_track_id&&mysql)params.set('target_track_id',p.target_track_id)
   const path=params.size?`${base}${base.includes('?')?'&':'?'}${params}`:base
   const targetPath=p.target_track_id?`${base}${base.includes('?')?'&':'?'}track_id=${p.target_track_id}`:null
   const [response, track, sources, report, targetReport, targetRows]=await Promise.all([
    request(path),
    p.track_id?request(`/tracks/${p.track_id}`).then(r=>r.json()):null,
    p.video_id?request(`/projects/${p.id}/videos`).then(r=>r.json()):[],
    p.track_id?request(`/tracks/${p.track_id}/quality-check`).then(r=>r.json()):null,
    p.target_track_id?request(`/tracks/${p.target_track_id}/quality-check`).then(r=>r.json()):null,
    targetPath?request(targetPath).then(r=>r.json()):[],
   ])
   const rows:Segment[]=await response.json()
   if(activeId.current===key&&editorRequest.current===revision){
    setActive(p)
    setSegments(rows)
    setTrackVersion(track?.version??null)
    setVideos(sources)
    setSubtitleTracks(available)
    setTargetSegments(targetRows)
    setIssues([...(report?.issues??[]),...(targetReport?.issues??[])])
   }
  }catch(error){setNotice((error as Error).message)}
  finally{if(activeId.current===key&&editorRequest.current===revision)setLoading(false)}
 }
 async function openSavedLiveProject(id:string){
  try{
   const project:Project=await request(`/projects/${id}`).then(response=>response.json())
   await openProject(project)
  }catch(error){setNotice((error as Error).message)}
 }
 useEffect(()=>{setPlayhead(0);setDuration(0)},[active?.id,active?.video_id])
 useEffect(()=>{
  setWaveform(null)
  if(!mysql||!active?.video_id||!active.media_ready)return
  const controller=new AbortController()
  request(`/videos/${active.video_id}/waveform`,{signal:controller.signal}).then(r=>r.json()).then(result=>{
   if(!controller.signal.aborted)setWaveform(result)
  }).catch(()=>{})
  return ()=>controller.abort()
 },[mysql,active?.video_id,active?.media_ready])
 useEffect(()=>{
  const language=active?.source_language
  setSource(language==='vi'||language==='en'?language:'auto')
  setTranslationLanguage(active?.target_language==='vi'||(!active?.target_language&&language==='en')?'vi':'en')
  setCaptionTrack(active?.target_language?'bilingual':'original')
 },[active?.id,active?.video_id,active?.source_language,active?.target_language])
 useEffect(()=>()=>cancelAnimationFrame(frame.current),[page,active?.id,active?.video_id])
 function syncPlayback(){
  cancelAnimationFrame(frame.current)
  const tick=()=>{
   const player=playerRef.current
   if(!player)return
   const instant=player.currentTime
   setPlayhead(previous=>Math.abs(previous-instant)>=0.04?instant:previous)
   if(!player.paused&&!player.ended)frame.current=requestAnimationFrame(tick)
  }
  tick()
 }
 function seek(seconds:number){
  const player=playerRef.current
  if(player&&player.readyState>0)player.currentTime=seconds
 }
 const processing=active?.status==='pending'||active?.status==='running'
 const hasActiveJobs = processing || projects.some(p=>p.status==='pending'||p.status==='running')
 useEffect(()=>{
  if(!hasActiveJobs)return
  let cancelled=false
  let polling=false
  const interval=window.setInterval(async()=>{
   if(polling)return
   polling=true
   try {
    const rows=await loadProjects()
    const current=active?.video_id&&processing?await request(projectUrl(active)).then(r=>r.json()):
     rows.find(p=>p.id===active?.id&&p.video_id===active?.video_id)
    if(cancelled||!active||!current||projectKey(current)!==activeId.current||
      (current.status===active.status&&current.media_ready===active.media_ready&&
       current.execution_environment===active.execution_environment&&current.progress===active.progress))return
    if(current.status==='completed'&&page==='editor')await openProject(current)
    else setActive(current)
   } catch(error){if(!cancelled)setNotice((error as Error).message)} finally {polling=false}
  },2000)
  return ()=>{cancelled=true;window.clearInterval(interval)}
 },[hasActiveJobs,active?.id,active?.video_id,active?.status,active?.media_ready,active?.execution_environment,active?.progress,page])
 const selectedJob=page==='history'?historyJob:page==='editor'?active?.job_id:null
 useEffect(()=>{
  setJob(null)
  if(!selectedJob)return
  let stopped=false
  let timeout:number|undefined
  async function poll(){
   try {
    const result:JobStatus=await request(`/jobs/${selectedJob}`).then(r=>r.json())
    if(stopped)return
    setJob(result)
    if(result.status==='PENDING'||result.status==='RUNNING')timeout=window.setTimeout(poll,1000)
   }catch(error){if(!stopped)setNotice((error as Error).message)}
  }
  void poll()
  return ()=>{stopped=true;window.clearTimeout(timeout)}
 },[selectedJob])
 useEffect(()=>{
  const project=historyId||projects[0]?.id
  setHistoryJob('')
  if(!mysql||page!=='history'||!project){setHistory([]);return}
  let cancelled=false
  request(`/projects/${project}/jobs`).then(r=>r.json()).then(rows=>{if(!cancelled)setHistory(rows)})
   .catch(error=>{if(!cancelled)setNotice(error.message)})
  return ()=>{cancelled=true}
 },[mysql,page,historyId,projects[0]?.id])
 function openCreate(){
  setCreateError('')
  setShowCreate(true)
 }
 async function createProject(customName=''){
  if(creating)return
  const sourceUrl=url.trim()
  if(!/^https?:\/\//i.test(sourceUrl)){
   setCreateError('Nhập URL video bắt đầu bằng https:// hoặc http://.')
   return
  }
  setCreateError('')
  setCreating(true)
  try{
   const parsed=new URL(sourceUrl)
   const videoId=parsed.searchParams.get('v')||parsed.pathname.split('/').filter(Boolean).pop()
   const name=customName.trim()||`Video · ${videoId||parsed.hostname}`.slice(0,255)
   const response=await request('/projects',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,source_url:sourceUrl,skip_asr:skipAsr}),
   })
   const project:Project=await response.json()
   setProjects(rows=>[project,...rows])
   setProjectName('')
   setUrl('')
   setShowCreate(false)
   await openProject(project)
  }catch(error){setCreateError((error as Error).message)}
  finally{setCreating(false)}
 }
 async function cancelJob(jobId:string){
  if(!confirm(`Hủy tác vụ #${jobId}?`))return
  try{
   await request(`/jobs/${jobId}/cancel`,{method:'POST'})
   setNotice('Đã hủy tác vụ.')
   await loadProjects()
  }catch(error){setNotice((error as Error).message)}
 }
 async function process(p:Project){
  if(p.source_type!=='URL'&&p.source_type!=='UPLOAD')return
  if(p.segment_count>0&&!confirm('Kết quả nhận dạng mới sẽ trở thành track đang hiển thị. Tiếp tục?'))return
  setLoading(true)
  try{
   const language=activeId.current===projectKey(p)?source:'auto'
   const response=await request(projectUrl(p,'/process',{
    source_language:language,accurate:String(accurate),force:String(p.status==='completed'),
   }),{method:'POST'})
   const job=await response.json()
   const rows=await loadProjects()
   const current=p.video_id?await request(projectUrl(p)).then(r=>r.json()):rows.find(row=>row.id===p.id)
   await openProject(current??p)
   setNotice(statuses[job.status]??job.status)
  }catch(e){setNotice((e as Error).message)}finally{setLoading(false)}
 }
 async function uploadFile(e:ChangeEvent<HTMLInputElement>, projectId?: string){
  const input=e.currentTarget
  const file=input.files?.[0]
  if(!file)return
  const body=new FormData()
  body.append('file',file)
  if(mysql&&skipAsr)body.append('skip_asr','true')
  setLoading(true)
  try{
   const path=projectId?`/projects/${projectId}/videos/upload`:'/upload'
   const response=await request(path,{method:'POST',body})
   const project:Project=await response.json()
   if(project.status==='pending'||(mysql&&skipAsr)){
    setProjects(rows=>[project,...rows.filter(row=>row.id!==project.id)])
    await openProject(project)
   }else{
    await process(project)
   }
  }catch(error){setNotice((error as Error).message)}
  finally{setLoading(false);input.value=''}
 }
 async function selectVideo(id:string){
  if(!active||id===active.video_id)return
  try{
   const project:Project=await request(projectUrl({...active,video_id:id})).then(r=>r.json())
   await openProject(project)
  }catch(error){setNotice((error as Error).message)}
 }
 async function addProjectUrl(event:FormEvent<HTMLFormElement>){
  event.preventDefault()
  if(!active||loading)return
  const data=new FormData(event.currentTarget)
  setLoading(true)
  try{
   const project:Project=await request(`/projects/${active.id}/videos/url`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:data.get('title'),source_url:data.get('source_url')}),
   }).then(r=>r.json())
   setShowSource(false)
   await openProject(project)
   void loadProjects()
  }catch(error){setNotice((error as Error).message)}
  finally{setLoading(false)}
 }
 async function saveSegments(rows:Segment[]){
  setSaving(true)
  try {
   let version=trackVersion
   for(const row of rows){
    const {id,source_text,translated_text,start_time,end_time}=row
    const saved=await request(`/segments/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({source_text,translated_text,start_time,end_time,base_version:version,
      target_track_id:active?.target_track_id})}).then(r=>r.json())
    if(typeof saved.version==='number')version=saved.version
    if(saved.translated_locked===true)setSegments(current=>current.map(item=>item.id===id?{...item,translated_locked:true}:item))
   }
   if(active?.track_id){
    setTrackVersion(version)
    const reports=await Promise.all([active.track_id,active.target_track_id].filter(Boolean).map(id=>
     request(`/tracks/${id}/quality-check`).then(r=>r.json())))
    setIssues(reports.flatMap(report=>report.issues??[]))
   }
   setNotice('Đã lưu thay đổi phụ đề.')
   return true
  } catch(e){setNotice((e as Error).message);return false} finally{setSaving(false)}
 }
 async function translate(){
  if(!active||translating)return
  if(source==='auto'||source===translationLanguage){
   setNotice('Chọn hai ngôn ngữ khác nhau cho bản gốc và bản dịch.')
   return
  }
  const projectId=active.id
  const selection=projectKey(active)
  setTranslating(true)
  try{
   if(!await saveSegments(segments))return
   const endpoint=active.track_id?`/tracks/${active.track_id}/translations`:`/projects/${projectId}/translate`
   const response=await request(endpoint,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({source_language:source,target_language:translationLanguage,mode:translationMode}),
   })
   let result=await response.json()
   if(active.track_id){
    const jobId=result.job_id
    for(;;){
     if(activeId.current!==selection)return
     const job=await request(`/jobs/${jobId}`).then(r=>r.json())
     if(job.status==='COMPLETED')break
     if(job.status==='FAILED'||job.status==='CANCELLED')throw new Error(job.error_message||'Tác vụ dịch thất bại.')
     await new Promise(resolve=>window.setTimeout(resolve,1500))
    }
    const updated:Project=await request(projectUrl(active)).then(r=>r.json())
    if(activeId.current===selection)await openProject(updated,active.track_id,translationLanguage)
    setCaptionTrack('bilingual')
    setNotice('Đã tạo phụ đề song ngữ.')
    return
   }
   if(activeId.current===selection){
    setSegments(result.segments)
    const updated=mysql?await request(projectUrl(active)).then(r=>r.json()):null
    if(activeId.current===selection)setActive(previous=>previous?{
     ...previous,...updated,source_language:source,target_language:result.target_language,
    }:previous)
    setCaptionTrack('bilingual')
   }
   setNotice('Đã tạo phụ đề song ngữ.')
  }catch(error){setNotice((error as Error).message)}
  finally{setTranslating(false)}
 }
 async function exportVideo(){
  if(!active?.media_ready||exportingVideo)return
  const key=`${projectKey(active)}/${active.track_id}/${active.target_track_id}/${captionTrack}`
  setExportingVideo(true)
  setVideoDownload(null)
  try{
   // Capture the editor state so changes are included even before the user saves them.
   const original=segments.map(segment=>({
    start_ms:Math.round(segment.start_time*1000),
    end_ms:Math.round(segment.end_time*1000),
    text:segment.source_text,
   }))
   const cues=captionTrack==='original'?original:captionTrack==='translation'?translationCues:bilingualCues(original,translationCues)
   const result:{download_url:string}=await request(projectUrl(active,'/video-exports'),exportVideoRequest(cues)).then(r=>r.json())
   const downloadUrl=`${API}${result.download_url}`
   setVideoDownload({key,url:downloadUrl})
   startVideoDownload(downloadUrl)
  }catch(error){setNotice((error as Error).message)}
  finally{setExportingVideo(false)}
 }
 async function inspectQuality(){
  if(!active?.track_id)return
  try {
   const reports=await Promise.all([active.track_id,active.target_track_id].filter(Boolean).map(id=>
    request(`/tracks/${id}/quality-check`,{method:'POST'}).then(r=>r.json())))
   const found=reports.flatMap(report=>report.issues??[])
   setIssues(found)
   setNotice(found.length?`Phát hiện ${found.length} cảnh báo.`:'Chưa phát hiện lỗi theo các ngưỡng kiểm tra.')
  }catch(error){setNotice((error as Error).message)}
 }
 async function importSubtitle(event:FormEvent<HTMLFormElement>){
  event.preventDefault()
  if(!active?.video_id||changingTrack)return
  setChangingTrack(true)
  const data=new FormData(event.currentTarget)
  if(!data.get('source_track_id'))data.delete('source_track_id')
  try{
   const track:{track_id:string}=await request(`/videos/${active.video_id}/tracks/import`,{method:'POST',body:data}).then(r=>r.json())
   const project:Project=await request(projectUrl(active)).then(r=>r.json())
   await openProject(project,track.track_id)
   setShowImport(false)
   setNotice('Đã nhập phụ đề vào track mới.')
   void loadProjects()
  }catch(error){setNotice((error as Error).message)}
  finally{setChangingTrack(false)}
 }
 async function renameProject(){
  if(!active)return
  const name=prompt('Tên dự án mới',active.name)?.trim()
  if(!name||name===active.name)return
  try{
   const project:Project=await request(`/projects/${active.id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name}),
   }).then(r=>r.json())
   setActive(current=>current&&current.id===project.id?{...current,name:project.name,description:project.description}:current)
   void loadProjects()
  }catch(error){setNotice((error as Error).message)}
 }
 async function renameProjectFor(project:Project){
  const name=prompt('Tên dự án mới',project.name)?.trim()
  if(!name||name===project.name)return
  try{
   await request(`/projects/${project.id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name}),
   })
   await loadProjects()
   setNotice('Đã đổi tên dự án.')
  }catch(error){setNotice((error as Error).message)}
 }
 async function exportFileFor(project:Project,format:'srt'|'vtt'){
  if(!project.track_id)return
  try{
   const response=await request(`/tracks/${project.track_id}/exports`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({format}),
   })
   const result=await response.json()
   const file=await request(`/exports/${result.export_id}/download`).then(r=>r.blob())
   const url=URL.createObjectURL(file)
   const anchor=document.createElement('a')
   anchor.href=url
   anchor.download=`${project.name}.${format}`
   anchor.click()
   URL.revokeObjectURL(url)
  }catch(error){setNotice((error as Error).message)}
 }
 async function toggleTranslationLock(segment:Segment){
  if(!active?.target_track_id||!segment.translated_id)return
  try{
   await request(`/tracks/${active.target_track_id}/segments/${segment.translated_id}/lock`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({locked:!segment.translated_locked}),
   })
   setSegments(rows=>rows.map(row=>row.id===segment.id?{
    ...row,translated_locked:!segment.translated_locked,
   }:row))
   setNotice(segment.translated_locked?'Đã mở khóa bản dịch.':'Đã khóa bản dịch để không bị ghi đè.')
  }catch(error){setNotice((error as Error).message)}
 }
 async function editStructure(row:Segment,index:number,operation:'delete'|'merge'|'split',boundary?:number){
  if(!active?.track_id||trackVersion===null||changingTrack)return
  let change:Record<string,string|number>={operation,segment_id:row.id}
  if(operation==='delete'&&!confirm('Xóa đoạn phụ đề này?'))return
  if(operation==='merge'){
   if(!segments[index+1])return
   change={...change,next_segment_id:segments[index+1].id}
  }
  if(operation==='split'){
   const words=row.words??[]
   const target=playhead>row.start_time&&playhead<row.end_time?playhead:(row.start_time+row.end_time)/2
   const breaks=Array.from(row.source_text.matchAll(/\s+/g)).map(match=>match.index??0)
   const marker=boundary??breaks.reduce((best,next)=>Math.abs(next/row.source_text.length-.5)<Math.abs(best/row.source_text.length-.5)?next:best,0)
   const left=row.source_text.slice(0,marker).trim(),right=row.source_text.slice(marker).trim()
   if(!left||!right||(/\S/.test(row.source_text[marker-1]??'')&&/\S/.test(row.source_text[marker]??''))){setNotice('Đặt con trỏ ở ranh giới hai từ để tách.');return}
   const normalize=(value:string)=>value.normalize('NFC').toLowerCase().match(/[\p{L}\p{N}]+/gu)?.join(' ')
   const matching=words.findIndex((_,i)=>i>0&&normalize(words.slice(0,i).map(word=>word.word_text).join(' '))===normalize(left))
   const at=matching>0?Math.round((words[matching-1].end_ms+words[matching].start_ms)/2):Math.round(target*1000)
   change={...change,left,right,at_ms:at}
  }
  setChangingTrack(true)
  try{
   await request(`/tracks/${active.track_id}/segments`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({baseVersion:trackVersion,operations:[change]}),
   })
   setIssues([])
   const project:Project=await request(projectUrl(active)).then(r=>r.json())
   await openProject(project,active.track_id,active.target_track_id)
   void loadProjects()
  }catch(error){setNotice((error as Error).message)}
  finally{setChangingTrack(false)}
 }
 async function removeProject(p:Project){if(!confirm(`Xóa dự án “${p.name}” cùng video và phụ đề?`))return;try{await request(`/projects/${p.id}`,{method:'DELETE'});setProjects(v=>v.filter(x=>x.id!==p.id));if(active?.id===p.id){activeId.current=null;setActive(null);setPage('projects')}setNotice('Đã xóa dự án.')}catch(e){setNotice((e as Error).message)}}
 function startLive(){const w=window as any;const Speech=w.SpeechRecognition||w.webkitSpeechRecognition;if(!Speech){setNotice('Trình duyệt chưa hỗ trợ Web Speech API. Hãy dùng Chrome hoặc Edge.');return}const rec=new Speech();rec.lang=demoLanguage;rec.continuous=true;rec.interimResults=true;rec.onresult=(ev:any)=>{let interim='';for(let i=ev.resultIndex;i<ev.results.length;i++){const t=ev.results[i][0].transcript;if(ev.results[i].isFinal)setLiveFinal(v=>[...v,t]);else interim+=t}setLiveText(interim)};rec.onerror=()=>setListening(false);rec.onend=()=>setListening(false);rec.start();(window as any).__liveRec=rec;setListening(true)}
 function stopLive(){(window as any).__liveRec?.stop();setListening(false)}
 const filtered=useMemo(()=>projects.filter(p=>p.name.toLowerCase().includes(query.toLowerCase())),[projects,query])
 const currentSegment=segments.find(s=>s.start_time<=playhead&&playhead<s.end_time)
 const translationCues=useMemo<VideoCue[]>(()=>{
  const drafts=segments.filter(row=>row.translated_id).map(row=>[row.translated_id,row] as const)
  const byId=new Map(drafts)
  if(targetSegments.length)return targetSegments.map(target=>{
   const draft=byId.get(target.id)
   return {start_ms:draft?.translated_start_ms??Math.round(target.start_time*1000),
    end_ms:draft?.translated_end_ms??Math.round(target.end_time*1000),text:draft?.translated_text??target.source_text}
  })
  return segments.filter(row=>row.translated_text).map(row=>({start_ms:row.translated_start_ms??Math.round(row.start_time*1000),
   end_ms:row.translated_end_ms??Math.round(row.end_time*1000),text:row.translated_text}))
 },[segments,targetSegments])
 const currentTranslation=translationCues.find(cue=>cue.start_ms<=playhead*1000&&playhead*1000<cue.end_ms)
 const editorBusy=loading||saving||translating||changingTrack||processing
 const nav=[{id:'overview',label:'Tổng quan',icon:LayoutDashboard},{id:'projects',label:'Dự án của tôi',icon:FolderKanban},{id:'editor',label:'Biên tập phụ đề',icon:Captions},{id:'live',label:'Phụ đề Live',icon:AudioLines},{id:'history',label:'Lịch sử xử lý',icon:Clock3}] as const
 return <div className="app-shell"><aside className="sidebar"><div className="logo"><span className="logo-icon"><Captions size={21}/></span><span>SubLearn<span className="logo-ai"> AI</span><small>VIDEO LANGUAGE STUDIO</small></span></div><div className="workspace"><div className="workspace-avatar">M</div><div><b>Không gian cá nhân</b><small>Gói nghiên cứu · Local AI</small></div><ChevronDown size={15}/></div><div className="nav-label">WORKSPACE</div><nav>{nav.map(n=><button key={n.id} className={`nav-item ${page===n.id?'active':''}`} onClick={()=>setPage(n.id)}><n.icon size={18}/>{n.label}{n.id==='live'&&<i className="live-pulse"/>}</button>)}{mysql&&account?.role==="ADMIN"&&<button className={page==="admin"?"nav-item active":"nav-item"} onClick={()=>setPage("admin")}><ShieldCheck size={18}/> Quản trị</button>}</nav><div className="side-bottom"><div className="local-card"><div className="local-icon"><ShieldCheck size={17}/></div><div><b>Chế độ AI cục bộ</b><small>Dữ liệu xử lý trên máy của bạn</small></div><span className="green-dot"/></div><button type="button" className={`sidebar-setting ${page==='settings'?'active':''}`} onClick={()=>setPage('settings')}><Settings2 size={17}/> Cài đặt</button><div className="profile"><div className="profile-avatar">{account?account.username.slice(0,2).toUpperCase():"K"}</div><div><b>{account?.username??"Khách"}</b>{account&&<small>{account.email}</small>}</div><MoreHorizontal size={18}/></div></div></aside>
 <main className="main"><input ref={fileRef} type="file" hidden accept="video/*,audio/*" onChange={uploadFile}/><header className="topbar"><div className="breadcrumb">SubLearn AI <span>/</span> <b>{nav.find(n=>n.id===page)?.label||'Workspace'}</b></div><div className="top-actions"><span className={`connection ${health?'online':''}`}><i/>{health?'AI Service đang hoạt động':'AI Service chưa kết nối'}</span><button className="icon-button"><CircleHelp size={18}/></button>{account?<><span className="account-name" title={account.email}>{account.username}</span><button type="button" className="secondary" onClick={signOut}>Đăng xuất</button></>:<><button type="button" className="secondary" onClick={()=>setShowRegister(true)}>Đăng ký</button><button type="button" className="primary" onClick={()=>setShowLogin(true)}>Đăng nhập</button></>}</div></header>
 {notice&&<div className="toast"><span>{notice}</span><button onClick={()=>setNotice('')}><X size={15}/></button></div>}
 {page==='overview'&&<div className="content"><div className="welcome-row"><div><div className="eyebrow"><Sparkles size={14}/> KHÔNG GIAN HỌC NGÔN NGỮ AI</div><h1>Xin chào, Minh <span>👋</span></h1><p>Biến mọi video thành cơ hội học một ngôn ngữ mới.</p></div><button className="primary" onClick={()=>overviewUrlRef.current?.focus()}><Plus size={17}/> Nhập URL video</button></div><section className="hero-card"><div className="hero-copy"><div className="hero-pill"><WandSparkles size={13}/> QUY TRÌNH PHỤ ĐỀ HỖ TRỢ AI</div><h2>Hiểu video.<br/><em>Chinh phục ngôn ngữ.</em></h2><form className="hero-url-form" onSubmit={e=>{e.preventDefault();void createProject()}}><label htmlFor="overview-video-url">URL video mạng xã hội</label><p id="overview-url-hint" className="mb-2">Facebook, TikTok, Douyin, YouTube, Instagram, X, Bilibili, Vimeo, Dailymotion</p><div className="hero-url-controls"><input id="overview-video-url" aria-describedby="overview-url-hint" ref={overviewUrlRef} type="url" inputMode="url" maxLength={2083} required value={url} onChange={e=>setUrl(e.target.value)} placeholder="Dán đường dẫn video công khai"/><button className="white-btn" type="submit" disabled={creating}>{creating?'Đang gửi…':skipAsr?'Tạo dự án':'Tạo phụ đề'} <span>→</span></button></div>{mysql&&<label className="quality-option"><input type="checkbox" checked={skipAsr} onChange={e=>setSkipAsr(e.target.checked)}/> Dùng tệp SRT/VTT có sẵn</label>}{!showCreate&&createError&&<p className="url-error" role="alert">{createError}</p>}</form><button className="ghost-btn hero-upload" type="button" disabled={loading} onClick={()=>fileRef.current?.click()}><Upload size={16}/> Tải video lên</button></div><div className="hero-visual"><div className="visual-orbit orbit-one"/><div className="visual-orbit orbit-two"/><div className="visual-core"><AudioLines size={46}/></div><div className="float-tag tag-top"><span className="tag-dot"/> Chuyển giọng nói thành văn bản</div><div className="float-tag tag-bottom"><Languages size={15}/> Sẵn sàng dịch</div><div className="wave-bars">{Array.from({length:28},(_,i)=><i key={i} style={{height:`${12+Math.abs(Math.sin(i*.7))*48}px`}}/>)}</div></div></section>
 <div className="section-heading"><div><h3>Không gian làm việc</h3><p>Theo dõi các hoạt động xử lý video của bạn</p></div><button className="text-btn" onClick={()=>setPage('projects')}>Xem tất cả <span>→</span></button></div><div className="stats-grid"><div className="stat-card"><div className="stat-icon purple"><FolderKanban size={19}/></div><span>Tổng dự án</span><strong>{projects.length}</strong><small>Video và bản ghi của bạn</small></div><div className="stat-card"><div className="stat-icon blue"><Captions size={19}/></div><span>Phụ đề đã tạo</span><strong>{projects.reduce((a,p)=>a+(p.segment_count||0),0)}</strong><small>Phân đoạn phụ đề</small></div><div className="stat-card"><div className="stat-icon orange"><Activity size={19}/></div><span>Trạng thái hệ thống</span><strong className="stat-status">{health?'Online':'Offline'}</strong><small>Local processing service</small></div></div>
 <div className="section-heading recent-heading"><div><h3>Dự án gần đây</h3><p>Tiếp tục công việc bạn đang thực hiện</p></div><button className="text-btn" onClick={()=>setPage('projects')}>Quản lý dự án <span>→</span></button></div><ProjectTable projects={projects.slice(0,4)} selected={[]} onSelectedChange={()=>{}} onOpen={openProject} onDelete={removeProject} onProcess={process}/></div>}
 {page==='projects'&&<div className="subtitle-library"><div className="library-title-row"><h1>Phụ đề</h1><div className="library-search"><Search size={18}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Tìm kiếm"/><kbd>Ctrl</kbd><kbd>K</kbd></div></div><div className="library-toolbar"><div className="library-actions"><button className="library-action muted" onClick={openCreate}><Plus size={16}/> Phụ đề mới</button><button className="library-action" onClick={()=>fileRef.current?.click()}><Upload size={16}/> Tải lên</button></div><button className="library-create" onClick={openCreate}><Plus size={18}/> Phụ đề mới</button></div>{selectedProjects.length>0&&<div className="library-selection"><button className="library-action" disabled><FolderInput size={16}/> Di chuyển</button><button className="library-action" disabled={selectedProjects.length!==1} onClick={()=>{const p=projects.find(row=>row.id===selectedProjects[0]);if(p)void renameProjectFor(p)}}><Pencil size={16}/> Đổi tên</button><button className="library-action" disabled><Copy size={16}/> Nhân bản</button><button className="library-action export-action" disabled={selectedProjects.length!==1} onClick={()=>{const p=projects.find(row=>row.id===selectedProjects[0]);if(p?.track_id)void exportFileFor(p,'srt')}}><ArrowDownToLine size={16}/> Xuất</button><button className="library-action delete-action" onClick={()=>{selectedProjects.forEach(id=>{const p=projects.find(row=>row.id===id);if(p)void removeProject(p)});setSelectedProjects([])}}><Trash2 size={16}/> Xóa</button><b>{selectedProjects.length} mục đã chọn</b><button className="library-clear" onClick={()=>setSelectedProjects([])}><X size={16}/></button></div>}<ProjectTable projects={filtered} selected={selectedProjects} onSelectedChange={setSelectedProjects} onOpen={openProject} onDelete={removeProject} onProcess={process}/></div>}
 {page==='editor'&&<div className="content">{!active?<Empty title="Chưa chọn dự án" desc="Chọn một dự án từ danh sách để biên tập phụ đề." action={()=>setPage('projects')}/>:<><div className="page-heading"><div><div className="eyebrow">SUBTITLE WORKBENCH</div><h1>{active.name}</h1><p>Chỉnh sửa nội dung, thời gian và xuất bản phụ đề.</p><p role="status">{statuses[active.status]}{active.execution_environment&&` · ${active.execution_environment==='GPU_SERVER'?'Remote GPU':active.execution_environment==='LOCAL'?'Local':active.execution_environment}`}{active.error_message&&` · ${active.error_message}`}</p></div><div className="heading-actions">{mysql&&active.track_id&&<button className="secondary" onClick={()=>setShowRevisions(true)}><Clock3 size={16}/> Phiên bản</button>}{mysql&&<button className="secondary" onClick={renameProject}>Đổi tên</button>}<label className="track-picker">Hiển thị và tải xuống<select value={captionTrack} onChange={e=>setCaptionTrack(e.target.value as Track)}><option value="original">Bản gốc</option><option value="translation" disabled={!translationCues.length}>Bản dịch</option><option value="bilingual" disabled={!translationCues.length}>Song ngữ</option></select></label><button className="primary" disabled={!active.media_ready||!segments.length||loading||processing||translating||exportingVideo}
  aria-busy={exportingVideo} onClick={exportVideo}>
  {exportingVideo?<LoaderCircle className="spin" size={16}/>:<ArrowDownToLine size={16}/>}
  {exportingVideo?'Đang tạo video…':'Tải video có phụ đề'}
 </button>
 {videoDownload?.key===`${projectKey(active)}/${active.track_id}/${active.target_track_id}/${captionTrack}`&&<a className="secondary small" href={videoDownload.url} download>Tải lại MP4</a>}</div></div>
 {mysql&&active.video_id&&<section className="control-form mb-3 flex flex-wrap items-center gap-3 rounded-xl bg-white p-4" aria-label="Track phụ đề">
  {subtitleTracks.length>0&&<label>Track đang biên tập
   <select value={active.track_id??''} disabled={editorBusy} onChange={e=>void openProject(active,e.target.value)}>
    {subtitleTracks.map(track=><option key={track.track_id} value={track.track_id}>{track.track_type==='TRANSLATION'?'Bản dịch':track.imported?'Phụ đề đã nhập':'Phụ đề nhận dạng'} · {track.language} · #{track.track_id}</option>)}
   </select>
  </label>}
  {subtitleTracks.filter(track=>track.source_track_id===active.track_id).length>1&&<label>Track song ngữ
   <select value={active.target_track_id??''} disabled={editorBusy} onChange={e=>void openProject(active,active.track_id,e.target.value)}>
    {subtitleTracks.filter(track=>track.source_track_id===active.track_id).map(track=><option key={track.track_id} value={track.track_id}>{track.language} · #{track.track_id}</option>)}
   </select>
  </label>}
  <button className="secondary small" disabled={editorBusy} onClick={()=>setShowImport(true)}><Upload size={15}/> Nhập SRT/VTT</button>
 </section>}
 {mysql&&<section className="control-form mb-3 rounded-xl bg-white p-4" aria-label="Nguồn trong dự án">
  <div className="flex flex-wrap items-center gap-2">
   <label className="flex items-center gap-2">Video
    <select value={active.video_id||''} disabled={loading||saving||translating||changingTrack} onChange={event=>void selectVideo(event.target.value)}>
     {!videos.length&&<option value="">Chưa có nguồn</option>}
     {videos.map(video=><option key={video.id} value={video.id}>{video.title} · {video.source_type}</option>)}
    </select>
   </label>
   <button className="secondary small" disabled={loading||saving||translating||changingTrack} onClick={()=>setShowSource(value=>!value)}><Plus size={15}/> Thêm URL</button>
   <label className="secondary small"><Upload size={15}/> Thêm tệp
    <input className="sr-only" type="file" aria-label="Thêm tệp vào dự án" accept="video/*,audio/*"
     disabled={loading||saving||translating||changingTrack} onChange={event=>uploadFile(event,active.id)}/>
   </label>
  </div>
  {showSource&&<form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={addProjectUrl}>
   <label className="grid gap-1">Tên video<input name="title" required maxLength={255}/></label>
   <label className="grid gap-1">URL video<input name="source_url" type="url" required maxLength={2083}/></label>
   <button className="primary small" type="submit" disabled={loading}>Tiếp nhận và tạo phụ đề</button>
  </form>}
 </section>}
 {job&&active.job_id===String(job.job_id)&&<JobSteps job={job}/>}
 <div className="editor-layout"><div className="video-panel">{active.media_ready?<div className="editor-player">
 <video key={projectKey(active)} ref={playerRef} src={`${API}${active.video_id?`/videos/${active.video_id}/media`:`/projects/${active.id}/media`}`} controls playsInline preload="metadata" onError={()=>setNotice('Trình duyệt không phát được định dạng video này.')} onLoadedMetadata={e=>setDuration(Number.isFinite(e.currentTarget.duration)?e.currentTarget.duration:0)} onTimeUpdate={e=>setPlayhead(e.currentTarget.currentTime)} onSeeked={e=>setPlayhead(e.currentTarget.currentTime)} onPlay={syncPlayback} />
 {((captionTrack!=='translation'&&currentSegment)||(captionTrack!=='original'&&currentTranslation))&&<div className="player-caption">{captionTrack!=='translation'&&currentSegment&&<span>{currentSegment.source_text}</span>}{captionTrack!=='original'&&currentTranslation&&<span className="caption-translation">{currentTranslation.text}</span>}</div>}
 </div>:<div className="video-placeholder"><div className="video-symbol"><Film size={34}/></div><b>{processing?'Đang chuẩn bị video':'Chưa có bản xem trước'}</b>{active.status==='failed'&&active.source_type==='URL'&&<><input ref={projectFileRef} type="file" hidden accept="video/*,audio/*" onChange={event=>uploadFile(event,active.id)}/><button type="button" className="secondary" disabled={loading} onClick={()=>projectFileRef.current?.click()}><Upload size={16}/> Tải tệp vào dự án này</button></>}</div>}<div className="video-meta"><span><FileVideo size={15}/>{active.source_type}</span><span><Clock3 size={15}/>{segments.length} phân đoạn</span></div><div className="quality-box"><div><ShieldCheck size={17}/><b>Kiểm tra chất lượng</b></div>{active.track_id&&<button className="secondary small" disabled={!segments.length||processing} onClick={inspectQuality}>Kiểm tra phụ đề</button>}{issues.length>0&&<p>{issues.length} cảnh báo. Các đoạn liên quan được đánh dấu bên cạnh.</p>}</div></div><div className="subtitle-panel"><div className="panel-head"><div><b>Danh sách phụ đề</b><small>{active.target_language?'Phụ đề song ngữ':'Phụ đề gốc'}</small></div><button className="secondary small" onClick={()=>process(active)} disabled={processing||loading||translating||active.source_type==='LIVE'||active.source_type==='DEMO'}><WandSparkles size={14}/>{active.status==='failed'?'Thử lại':active.status==='completed'?'Nhận dạng lại':'Tạo phụ đề'}</button></div>
 {active.source_type!=='DEMO'&&<div className="subtitle-options">
  <label>Ngôn ngữ gốc<select value={source} onChange={e=>setSource(e.target.value as Language|'auto')} disabled={translating}><option value="auto">Tự nhận diện</option><option value="vi">Tiếng Việt</option><option value="en">Tiếng Anh</option></select></label>
  <label>Ngôn ngữ đích<select value={translationLanguage} onChange={e=>setTranslationLanguage(e.target.value as Language)} disabled={translating}><option value="en">Tiếng Anh</option><option value="vi">Tiếng Việt</option></select></label>
  <label>Chế độ dịch<select value={translationMode} disabled={translating}
   onChange={event=>setTranslationMode(event.target.value as typeof translationMode)}>
   <option value="local">Dịch nhanh</option>
   <option value="beam">Ưu tiên chất lượng</option>
   {contextAvailable&&<option value="context">Dịch theo ngữ cảnh</option>}
  </select></label>
  {mysql&&<button className="secondary small" disabled={source==='auto'||source===translationLanguage||translating}
   onClick={()=>setShowGlossary(true)}>Thuật ngữ</button>}
  <label className="quality-option"><input type="checkbox" checked={accurate} onChange={e=>setAccurate(e.target.checked)}/> Ưu tiên độ chính xác nhận dạng</label>
  <button className="primary small" disabled={processing||loading||saving||translating||!segments.length} onClick={translate}><Languages size={14}/>{translating?'Đang dịch…':active.target_language?'Dịch lại phụ đề':'Dịch phụ đề'}</button>
 </div>}
 {loading?<div className="loading"><LoaderCircle className="spin"/> Đang tải...</div>:processing?<div className="loading" role="status"><LoaderCircle className="spin"/> {statuses[active.status]}…</div>:segments.length===0?<div className="loading">'Nhập SRT/VTT hoặc tạo phụ đề từ video.'</div>:<div className="segment-list">{segments.map((s,i)=><SegmentEditor showTranslation={!!active.target_language||active.source_type==='DEMO'} editingDisabled={translating||changingTrack||saving} issueLabels={issues.filter(issue=>issue.segment_id===s.id||issue.segment_id===s.translated_id).map(issue=>issueHints[issue.issue_type]??issue.issue_type)} onStructure={active.track_id?((operation,boundary)=>editStructure(s,i,operation,boundary)):undefined} canMerge={i+1<segments.length} current={currentSegment?.id===s.id} onSeek={()=>seek(s.start_time)} key={s.id} segment={s} index={i} onSave={row=>saveSegments([row])} onToggleLock={()=>void toggleTranslationLock(s)} onChange={next=>setSegments(v=>v.map(x=>x.id===s.id?next:x))}/>)}</div>}<div className="save-row"><span>{saving?'Đang lưu...':'Các thay đổi được lưu riêng từng đoạn'}</span><button className="primary" disabled={saving||translating||processing||!segments.length} onClick={()=>saveSegments(segments)}><Check size={15}/> Lưu thay đổi</button></div></div></div>{active.media_ready&&<SubtitleTimeline segments={segments} duration={duration||(active.duration_ms??0)/1000} position={playhead} currentId={currentSegment?.id}
  waveform={waveform} editable={!editorBusy} onSeek={seek} onMove={(id,start,end)=>setSegments(rows=>rows.map(row=>row.id===id?{
   ...row,start_time:start,end_time:end,words:[],
   ...(row.translated_start_ms===Math.round(row.start_time*1000)&&row.translated_end_ms===Math.round(row.end_time*1000)
    ?{translated_start_ms:Math.round(start*1000),translated_end_ms:Math.round(end*1000)}:{}),
  }:row))}/>}</>}</div>}
 {page==='live'&&mysql&&<LivePanel api={API} projects={projects} signedIn={Boolean(account)} contextAvailable={contextAvailable}
  onLogin={()=>setShowLogin(true)} onOpenProject={openSavedLiveProject} onProjectsChanged={()=>{void loadProjects()}}/>}
 {page==='live'&&!mysql&&<div className="content"><div className="page-heading"><div><div className="eyebrow"><span className="live-pulse"/> REAL-TIME CAPTION</div><h1>Phụ đề Live</h1><p>Nhận dạng giọng nói từ microphone và hiển thị văn bản gần thời gian thực.</p></div><span className="beta-tag">THỬ NGHIỆM · NHẬN DẠNG TRÌNH DUYỆT</span></div><div className="live-layout"><div className="live-control"><div className="mic-orb"><div><Mic size={34}/></div></div><h2>{listening?'Đang lắng nghe...':'Sẵn sàng ghi nhận giọng nói'}</h2><p>{listening?'Hãy nói rõ ràng. Văn bản sẽ xuất hiện ở khung bên phải.':'Cho phép trình duyệt truy cập microphone để bắt đầu.'}</p><div className="language-select"><label>Ngôn ngữ đầu vào</label><select value={demoLanguage} disabled={listening} onChange={event=>setDemoLanguage(event.target.value)}><option value="en-US">English (United States)</option><option value="vi-VN">Tiếng Việt</option><option value="fr-FR">Français</option></select></div>{!listening?<button className="primary wide" onClick={startLive}><Mic size={17}/> Bắt đầu Live</button>:<button className="stop-btn wide" onClick={stopLive}>Dừng ghi âm</button>}<small className="privacy-note"><ShieldCheck size={14}/> Chế độ demo dùng tính năng nhận dạng của trình duyệt.</small></div><div className="live-transcript"><div className="panel-head"><div><b>Bản ghi trực tiếp</b><small>{liveFinal.length} câu đã hoàn tất</small></div><span className={`recording-state ${listening?'recording':''}`}><i/>{listening?'REC':'READY'}</span></div><div className="transcript-body">{liveFinal.length===0&&!liveText?<div className="transcript-empty"><AudioLines size={32}/><b>Chưa có nội dung</b><span>Bắt đầu nói để xem phụ đề tại đây.</span></div>:<>{liveFinal.map((t,i)=><p className="transcript-line" key={i}><small>{String(i+1).padStart(2,'0')}</small>{t}</p>)}{liveText&&<p className="transcript-line interim"><small>••</small>{liveText}</p>}</>}</div><div className="transcript-footer"><button className="text-btn" onClick={()=>{setLiveFinal([]);setLiveText('')}}>Xóa bản ghi</button><span>Ngôn ngữ nhận dạng {demoLanguage}</span></div></div></div></div>}
 {page==='history'&&<div className="content">
  <div className="page-heading"><h1>Lịch sử xử lý</h1></div>
  {mysql?<><div className="control-form mb-3 rounded-xl bg-white p-4">
   <label className="flex flex-wrap items-center gap-2">Dự án
    <select value={historyId||projects[0]?.id||''} onChange={event=>setHistoryId(event.target.value)}>
     {projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
    </select>
   </label>
   {history.length?<ul className="mt-2 grid gap-2">
    {history.map(item=><li key={item.job_id}><button className={historyJob===item.job_id?'primary small':'secondary small'} onClick={()=>setHistoryJob(item.job_id)}>
     #{item.job_id} · {item.title||item.job_type} · {statuses[item.status.toLowerCase()]??item.status} · {item.progress}%
    </button></li>)}
   </ul>:<p className="mt-2">Chưa có tác vụ.</p>}
  </div>{job&&<JobSteps job={job} onCancel={()=>void cancelJob(String(job.job_id))}/>}</>:<ProjectTable projects={projects} selected={[]} onSelectedChange={()=>{}} onOpen={openProject} onDelete={removeProject} onProcess={process}/>} 
 </div>}
 {page==='settings'&&<div className="content">
  <div className="page-heading"><h1>Cấu hình AI</h1></div>
  <div className="settings-grid">
   <div className="setting-card"><div className="setting-icon"><Languages/></div><div>
    <h3>Ngôn ngữ mặc định</h3>
    <label className="settings-field">Ngôn ngữ gốc
     <select value={source} onChange={event=>setSource(event.target.value as Language|'auto')}><option value="auto">Tự nhận diện</option><option value="vi">Tiếng Việt</option><option value="en">Tiếng Anh</option></select>
    </label>
    <label className="settings-field">Ngôn ngữ dịch
     <select value={translationLanguage} onChange={event=>setTranslationLanguage(event.target.value as Language)}><option value="vi">Tiếng Việt</option><option value="en">Tiếng Anh</option></select>
    </label>
   </div></div>
   <div className="setting-card"><div className="setting-icon"><AudioLines/></div><div>
    <h3>Nhận dạng giọng nói</h3><p>{speechRouting==='remote_gpu'?'Worker GPU và xử lý dự phòng trên máy':speechRouting?'Whisper trên máy':'Đang kết nối máy chủ'}</p>
    {mysql&&speechRouting&&<p>{dialectAvailable?'Giọng Bắc, Trung, Nam · Sẵn sàng trên máy':'Chưa có mô hình vùng giọng trên máy'}</p>}
   </div></div>
   <div className="setting-card"><div className="setting-icon"><Languages/></div><div>
    <h3>Xử lý dịch</h3>
    <label className="settings-field">Chế độ dịch
     <select value={translationMode} onChange={event=>setTranslationMode(event.target.value as typeof translationMode)}><option value="local">Dịch nhanh</option><option value="beam">Ưu tiên chất lượng</option>{contextAvailable&&<option value="context">Dịch theo ngữ cảnh</option>}</select>
    </label>
    <label className="settings-check"><input type="checkbox" checked={accurate} onChange={event=>setAccurate(event.target.checked)}/> Ưu tiên độ chính xác nhận dạng</label>
   </div></div>
   <div className="setting-card"><div className="setting-icon"><Film/></div><div>
    <h3>Nguồn video</h3><p>URL mạng xã hội và tệp tải lên</p>
   </div></div>
   <div className="setting-card"><div className="setting-icon"><Globe2/></div><div>
    <h3>Cơ sở dữ liệu</h3><p>{mysql?'MySQL':'SQLite'}</p>
    {mysql&&account?.role==='ADMIN'&&<button className="secondary small" onClick={()=>setPage('admin')}>Mở quản trị</button>}
   </div></div>
  </div>
 </div>}
 {page==="admin"&&mysql&&account?.role==="ADMIN"&&<AdminPanel api={API}/>}<footer className="app-footer">© 2026 SUBLEARN AI <span>•</span> GRADUATION PROJECT <span className="footer-right">LOCAL-FIRST · PRIVACY BY DESIGN</span></footer></main>
 {showRevisions&&page==='editor'&&active?.track_id&&<RevisionExport
  key={projectKey(active)} api={API} exportPath={projectUrl(active,'/video-exports')} mediaReady={Boolean(active.media_ready)}
  originalId={active.track_id} translationId={active.target_track_id}
  onClose={()=>setShowRevisions(false)}/>}
 {showGlossary&&page==='editor'&&source!=='auto'&&<GlossaryPanel api={API} source={source}
  target={translationLanguage} onClose={()=>setShowGlossary(false)}/>}
 {showLogin&&<Login api={API} onClose={()=>setShowLogin(false)} onLogin={user=>{setAccount(user);setShowLogin(false);setNotice("Đã đăng nhập.");void loadProjects()}}/>}
 {showRegister&&<Register api={API} onClose={()=>setShowRegister(false)} onRegistered={username=>{setShowRegister(false);setShowLogin(true);setNotice("Đã tạo tài khoản "+username+".")}}/>}
 {showImport&&active?.video_id&&<div className="modal-backdrop" onClick={()=>{if(!changingTrack)setShowImport(false)}}>
  <form className="modal" role="dialog" aria-modal="true" aria-labelledby="import-title" onClick={e=>e.stopPropagation()} onSubmit={importSubtitle}>
   <div className="modal-title"><h2 id="import-title">Nhập phụ đề SRT/VTT</h2><button type="button" className="icon-button" aria-label="Đóng" disabled={changingTrack} onClick={()=>setShowImport(false)}><X size={18}/></button></div>
   <label htmlFor="subtitle-import-file">Tệp phụ đề</label>
   <input id="subtitle-import-file" name="file" type="file" accept=".srt,.vtt" required/>
   <label htmlFor="subtitle-import-language">Ngôn ngữ</label>
   <select id="subtitle-import-language" name="language" defaultValue={translationLanguage}><option value="vi">Tiếng Việt</option><option value="en">Tiếng Anh</option></select>
   <label htmlFor="subtitle-import-source">Track nguồn</label>
   <select id="subtitle-import-source" name="source_track_id"><option value="">Track độc lập</option>
    {subtitleTracks.map(track=><option key={track.track_id} value={track.track_id}>{track.language} · #{track.track_id}</option>)}
   </select>
   <div className="modal-actions"><button type="submit" className="primary" disabled={changingTrack}><Upload size={16}/> {changingTrack?'Đang nhập…':'Nhập phụ đề'}</button></div>
  </form>
 </div>}
 {showCreate&&<div className="modal-backdrop" onClick={()=>{if(!creating)setShowCreate(false)}}>
  <form className="modal" role="dialog" aria-modal="true" aria-labelledby="create-title" onClick={e=>e.stopPropagation()} onSubmit={e=>{e.preventDefault();void createProject(projectName)}}>
   <div className="modal-title"><h2 id="create-title">Tạo dự án từ URL</h2><button type="button" className="icon-button" aria-label="Đóng" disabled={creating} onClick={()=>setShowCreate(false)}><X size={18}/></button></div>
   <label htmlFor="project-video-url">URL video (bắt buộc)</label>
   <input id="project-video-url" type="url" inputMode="url" maxLength={2083} required autoFocus={!url} value={url} onChange={e=>setUrl(e.target.value)} placeholder="Dán đường dẫn video công khai"/>
   <label htmlFor="project-name">Tên dự án (tùy chọn)</label>
   <input id="project-name" autoFocus={!!url} maxLength={255} value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="Ví dụ: Luyện nghe tiếng Anh"/>
   {mysql&&<label className="quality-option"><input type="checkbox" checked={skipAsr} onChange={e=>setSkipAsr(e.target.checked)}/> Dùng tệp SRT/VTT có sẵn</label>}
   {createError&&<p className="modal-error" role="alert">{createError}</p>}
   <div className="modal-actions"><button type="button" className="secondary" disabled={creating} onClick={()=>setShowCreate(false)}>Hủy</button><button className="primary" type="submit" disabled={creating}><Plus size={16}/>{creating?'Đang tạo...':'Tạo dự án'}</button></div>
  </form>
 </div>}
 </div>
}

function ProjectTable({projects,selected,onSelectedChange,onOpen,onDelete,onProcess}:{projects:Project[];selected:string[];onSelectedChange:(ids:string[])=>void;onOpen:(p:Project)=>void;onDelete:(p:Project)=>void;onProcess:(p:Project)=>void}){
 const allSelected=projects.length>0&&projects.every(project=>selected.includes(project.id))
 const toggleAll=()=>onSelectedChange(allSelected?[]:projects.map(project=>project.id))
 const toggle=(id:string)=>onSelectedChange(selected.includes(id)?selected.filter(value=>value!==id):[...selected,id])
 const duration=(value:number|null|undefined)=>{
  if(!value||value<=0)return '0:00'
  const total=Math.floor(value/1000)
  return `${Math.floor(total/60)}:${String(total%60).padStart(2,'0')}`
 }
 const date=(value:string)=>new Date(value).toLocaleDateString('vi-VN',{day:'2-digit',month:'2-digit',year:'numeric'})
 return <div className="subtitle-table"><div className="subtitle-table-head"><label className="library-checkbox"><input type="checkbox" checked={allSelected} onChange={toggleAll}/><span/></label><b>TÊN</b><b>THỜI LƯỢNG</b><b>ĐÃ TẢI LÊN</b><b>CHỈNH SỬA <ChevronDown size={14}/></b><b>TRẠNG THÁI</b></div>{projects.length===0?<div className="library-empty"><FolderKanban size={30}/><b>Chưa có phụ đề</b><span>Tạo dự án hoặc tải video để bắt đầu.</span></div>:projects.map(project=><div className={`subtitle-table-row ${selected.includes(project.id)?'selected':''}`} key={project.id}><label className="library-checkbox"><input type="checkbox" checked={selected.includes(project.id)} onChange={()=>toggle(project.id)}/><span/></label><button className="subtitle-name" onClick={()=>onOpen(project)}><span className="subtitle-icon"><Film size={17}/></span><span><b>{project.video_title||project.name}</b>{project.segment_count>0&&<small>{project.segment_count} segments</small>}</span></button><span className="subtitle-duration">{duration(project.duration_ms)}</span><span>{date(project.created_at)}</span><span>{date(project.created_at)}</span><span className={`subtitle-status ${project.status==='failed'?'failed':'ready'}`}><i>{project.status==='failed'?'!':'✓'}</i>{project.status==='failed'?'Thất bại':'Sẵn sàng'}</span></div>)}</div>
}
function SegmentEditor({segment,index,onSave,onChange,showTranslation,editingDisabled,current,onSeek,issueLabels,onStructure,canMerge,onToggleLock}:{segment:Segment;index:number;showTranslation:boolean;editingDisabled:boolean;issueLabels:string[];onStructure?:(operation:'split'|'merge'|'delete',boundary?:number)=>void;canMerge:boolean;current:boolean;onSeek:()=>void;onSave:(s:Segment)=>void;onChange:(s:Segment)=>void;onToggleLock?:()=>void}){return <div className={`segment-card ${current?'segment-current':''}`}><div className="segment-top"><span className="segment-number">#{String(index+1).padStart(2,'0')}</span><button type="button" className="segment-time segment-seek" onClick={onSeek}>{time(segment.start_time)} – {time(segment.end_time)}</button>{segment.speaker_label&&<span className="confidence">{segment.speaker_label}</span>}
 {segment.dialect_label&&dialectLabels[segment.dialect_label]&&<span className="confidence" title="Điểm phân loại vùng giọng">
  {dialectLabels[segment.dialect_label]}{segment.dialect_confidence!=null&&<> · {Math.round(segment.dialect_confidence*100)}%</>}
 </span>}
 {segment.confidence!==null&&<span className="confidence" title="Điểm nhận dạng lời thoại">{Math.round(segment.confidence*100)}%</span>}</div><label>Phụ đề gốc</label>{issueLabels.length>0&&<p className="segment-warning" role="status">{issueLabels.join(" · ")}</p>}<textarea disabled={editingDisabled} value={segment.source_text} onChange={e=>onChange({...segment,source_text:e.target.value})}
 onKeyDown={event=>{if(onStructure&&event.key==='Enter'&&!event.shiftKey&&!event.nativeEvent.isComposing){
  event.preventDefault()
  onStructure('split',event.currentTarget.selectionStart)
 }}}/>{showTranslation&&<><div className="segment-label-row"><label>Bản dịch</label>{onToggleLock&&segment.translated_id&&<button type="button" className="translation-lock" onClick={onToggleLock}>{segment.translated_locked?<Lock size={14}/>:<LockOpen size={14}/>} {segment.translated_locked?'Đang khóa':'Khóa bản dịch'}</button>}</div><textarea disabled={editingDisabled} value={segment.translated_text} onChange={e=>onChange({...segment,translated_text:e.target.value})}/></>}<div className="time-edit"><label>Bắt đầu (giây)<input type="number" min="0" step="0.001" disabled={editingDisabled} value={segment.start_time} onChange={e=>onChange({...segment,start_time:Number(e.target.value)})}/></label><label>Kết thúc (giây)<input type="number" min="0" step="0.001" disabled={editingDisabled} value={segment.end_time} onChange={e=>onChange({...segment,end_time:Number(e.target.value)})}/></label><button className="save-segment" disabled={editingDisabled} onClick={()=>onSave(segment)}><Check size={14}/> Lưu</button></div>{onStructure&&<div className="mt-2.5 flex flex-wrap gap-2"><button className="secondary small" disabled={editingDisabled} onClick={()=>onStructure('split')}>Tách (Enter)</button><button className="secondary small" disabled={editingDisabled||!canMerge} onClick={()=>onStructure('merge')}>Gộp với đoạn sau</button><button className="secondary small" disabled={editingDisabled} onClick={()=>onStructure('delete')}>Xóa</button></div>}</div>}
function Empty({title,desc,action}:{title:string;desc:string;action:()=>void}){return <div className="empty-state"><Captions size={30}/><b>{title}</b><span>{desc}</span><button className="secondary" onClick={action}>Đi đến dự án</button></div>}
