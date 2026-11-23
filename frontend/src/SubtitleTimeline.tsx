import {useEffect, useRef, type PointerEvent} from 'react'

type Cue = {id:string;start_time:number;end_time:number;source_text:string;translated_text:string}
export type Waveform = {peaks:number[];duration_ms:number;bucket_ms?:number}

export default function SubtitleTimeline({segments,duration,position,currentId,waveform,editable,onSeek,onMove}:{
 segments:Cue[];duration:number;position:number;currentId?:string;waveform:Waveform|null;editable:boolean;
 onSeek:(seconds:number)=>void;onMove:(id:string,start:number,end:number)=>void
}) {
 const length=segments.reduce((last,cue)=>Math.max(last,cue.end_time),duration)
 const scroller=useRef<HTMLDivElement>(null)
 const track=useRef<HTMLDivElement>(null)
 const canvas=useRef<HTMLCanvasElement>(null)
 const drag=useRef<{id:string;x:number;start:number;end:number;min:number;max:number;moved:boolean}|null>(null)
 useEffect(()=>{
  const node=canvas.current
  const context=node?.getContext('2d')
  if(!node||!context)return
  context.clearRect(0,0,node.width,node.height)
  if(!waveform?.peaks.length||!length)return
  context.fillStyle='#8274d0'
  const step=(waveform.bucket_ms??waveform.duration_ms/waveform.peaks.length)/1000/length*node.width
  waveform.peaks.forEach((peak,i)=>{
   const height=Math.max(1,Math.sqrt(peak)*node.height)
   context.fillRect(i*step,(node.height-height)/2,Math.max(1,step*.65),height)
  })
 },[waveform,length])
 useEffect(()=>{
  const pane=scroller.current, lane=track.current
  if(!pane||!lane||!length||drag.current)return
  const pixel=position/length*lane.clientWidth
  if(pixel<pane.scrollLeft||pixel>pane.scrollLeft+pane.clientWidth-15){
   pane.scrollLeft=Math.max(0,pixel-pane.clientWidth*.2)
  }
 },[position,length])
 function begin(event:PointerEvent<HTMLButtonElement>,cue:Cue){
  if(!editable||event.button!==0)return
  const left=segments.reduce((bound,s)=>s.id!==cue.id&&s.end_time<=cue.start_time?Math.max(bound,s.end_time):bound,0)
  const right=segments.reduce((bound,s)=>s.id!==cue.id&&s.start_time>=cue.end_time?Math.min(bound,s.start_time):bound,length)
  drag.current={id:cue.id,x:event.clientX,start:cue.start_time,end:cue.end_time,min:left,max:right,moved:false}
  event.currentTarget.setPointerCapture(event.pointerId)
 }
 function move(event:PointerEvent<HTMLButtonElement>){
  const item=drag.current, lane=track.current
  if(!item||!lane||(!item.moved&&Math.abs(event.clientX-item.x)<4))return
  item.moved=true
  const delta=(event.clientX-item.x)/lane.clientWidth*length
  const shift=Math.min(item.max-item.end,Math.max(item.min-item.start,delta))
  onMove(item.id,Math.round((item.start+shift)*1000)/1000,Math.round((item.end+shift)*1000)/1000)
 }
 if(!length)return null
 const time=(seconds:number)=>`${Math.floor(seconds/60)}:${(seconds%60).toFixed(2).padStart(5,'0')}`
 return <section className="editor-timeline" aria-label="Dòng thời gian phụ đề">
  <div className="timeline-header"><b>Dòng thời gian</b><span>{time(position)} / {time(length)}</span></div>
  <input type="range" min={0} max={length} step={0.001} value={Math.min(position,length)} aria-label="Vị trí phát" onChange={e=>onSeek(Number(e.target.value))}/>
  <div className="timeline-scroll" ref={scroller}>
   <div className="timeline-track" ref={track} style={{width:`max(100%, ${Math.min(length*28,40000)}px)`}}>
    <canvas className="timeline-waveform" width={2400} height={42} ref={canvas} aria-label="Dạng sóng âm thanh"
     onClick={event=>{const bounds=event.currentTarget.getBoundingClientRect();onSeek((event.clientX-bounds.left)/bounds.width*length)}}/>
    {segments.filter(cue=>cue.start_time>=0&&cue.end_time>cue.start_time).map(cue=>
     <button key={cue.id} type="button" title={cue.source_text} aria-label={`${time(cue.start_time)} ${cue.source_text}`}
      aria-current={cue.id===currentId?'true':undefined} onPointerDown={event=>begin(event,cue)} onPointerMove={move}
      onPointerUp={()=>{if(!drag.current?.moved)onSeek(cue.start_time);drag.current=null}}
      onPointerCancel={()=>{drag.current=null}} onLostPointerCapture={()=>{drag.current=null}}
      onClick={event=>{if(event.detail===0)onSeek(cue.start_time)}}
      style={{left:`${cue.start_time/length*100}%`,width:`${(cue.end_time-cue.start_time)/length*100}%`}}>{cue.source_text}</button>)}
    <span className="timeline-playhead" style={{left:`${Math.min(position/length,1)*100}%`}}/>
   </div>
  </div>
 </section>
}
