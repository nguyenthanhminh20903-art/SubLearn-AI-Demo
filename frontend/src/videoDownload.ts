export type VideoCue = {start_ms: number; end_ms: number; text: string}

export function bilingualCues(original: VideoCue[], translated: VideoCue[]): VideoCue[] {
  const cues = [...original, ...translated]
  const events = cues.flatMap((cue, i) => [{at:cue.start_ms, i, start:true}, {at:cue.end_ms, i, start:false}])
    .sort((a,b) => a.at-b.at || Number(a.start)-Number(b.start))
  const active = [new Set<number>(), new Set<number>()]
  const output: VideoCue[] = []
  let previous = events[0]?.at ?? 0
  for (const event of events) {
    if (event.at > previous) {
      const text = active.flatMap(track => [...track].map(i => cues[i].text.trim())).filter(Boolean).join('\n')
      const last = output.at(-1)
      if (text && last?.end_ms === previous && last.text === text) last.end_ms = event.at
      else if (text) output.push({start_ms:previous, end_ms:event.at, text})
    }
    const track = active[Number(event.i >= original.length)]
    if (event.start) track.add(event.i)
    else track.delete(event.i)
    previous = event.at
  }
  return output
}

export function exportVideoRequest(cues: VideoCue[]): RequestInit {
  const nonEmpty = cues.filter(cue => cue.text.trim())
  if (!nonEmpty.length) throw new Error('Chưa có phụ đề để gắn vào video.')
  if (nonEmpty.some(cue => !Number.isSafeInteger(cue.start_ms) || !Number.isSafeInteger(cue.end_ms) ||
    cue.start_ms < 0 || cue.end_ms <= cue.start_ms)) {
    throw new Error('Kiểm tra thời gian bắt đầu và kết thúc của các đoạn phụ đề.')
  }
  return {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cues: nonEmpty}),
  }
}

export function startVideoDownload(url: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  document.body.append(link)
  link.click()
  link.remove()
}
