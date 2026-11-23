import {readFileSync} from 'node:fs'
import * as ort from 'onnxruntime-web'

ort.env.wasm.numThreads = Math.max(1, Math.min(4, globalThis.navigator?.hardwareConcurrency || 2))

const {files, inputs, config, beams} = JSON.parse(readFileSync(0, 'utf8'))
const options = {executionProviders: ['wasm']}
const encoder = await ort.InferenceSession.create(files.encoder, options)
const decoder = await ort.InferenceSession.create(files.decoder, options)
const {pad_token_id: pad, eos_token_id: eos, decoder_start_token_id: start} = config

function intTensor(rows) {
  return new ort.Tensor('int64', BigInt64Array.from(rows.flat(), BigInt), [rows.length, rows[0].length])
}

function repeatHidden(hidden, count) {
  const data = new Float32Array(count * hidden.data.length)
  for (let row = 0; row < count; row++) data.set(hidden.data, row * hidden.data.length)
  return new ort.Tensor('float32', data, [count, ...hidden.dims.slice(1)])
}

function candidates(values) {
  const top = []
  const limit = beams * 2
  for (let id = 0; id < values.length; id++) {
    if (id === pad) continue
    const score = values[id]
    if (top.length === limit && score <= top.at(-1).score) continue
    const at = top.findIndex(item => score > item.score)
    top.splice(at < 0 ? top.length : at, 0, {id, score})
    if (top.length > limit) top.pop()
  }
  if (beams === 1) return [top[0]]
  const peak = top[0].score
  let sum = 0
  for (let id = 0; id < values.length; id++) {
    if (id !== pad) sum += Math.exp(values[id] - peak)
  }
  const total = peak + Math.log(sum)
  return top.map(({id, score}) => ({id, score: score - total}))
}

async function decode(input, hidden, mask) {
  const beamHidden = beams > 1 ? repeatHidden(hidden, beams) : hidden
  const beamMask = beams > 1 ? intTensor(Array(beams).fill(Array(input.length).fill(1))) : mask
  const maxTokens = Math.min(160, input.length * 3 + 16)
  let running = [{ids: [start], score: 0}]
  let completed = []
  try {
    for (let step = 1; step <= maxTokens; step++) {
      const ids = intTensor(running.map(item => item.ids))
      const single = running.length === 1
      const {logits} = await decoder.run({
        input_ids: ids,
        encoder_hidden_states: single ? hidden : beamHidden,
        encoder_attention_mask: single ? mask : beamMask,
      }, ['logits'])
      ids.dispose()
      const [, length, vocabulary] = logits.dims
      const proposals = running.flatMap((item, row) => {
        const offset = (row * length + length - 1) * vocabulary
        return candidates(logits.data.subarray(offset, offset + vocabulary)).map(token => ({
          id: token.id, ids: [...item.ids, token.id], score: item.score + token.score,
        }))
      }).sort((left, right) => right.score - left.score)
      logits.dispose()
      if (beams === 1 && proposals[0].id === eos) return proposals[0].ids.slice(1)
      completed.push(...proposals.slice(0, beams).filter(item => item.id === eos)
        .map(item => ({ids: item.ids, score: item.score / step})))
      completed = completed.sort((left, right) => right.score - left.score).slice(0, beams)
      running = proposals.filter(item => item.id !== eos).slice(0, beams)
      if (!running.length || (completed.length === beams && completed.at(-1).score >= running[0].score / step)) {
        return completed[0].ids.slice(1)
      }
    }
    const best = completed[0]
    return (best && best.score >= running[0].score / maxTokens ? best.ids : running[0].ids).slice(1)
  } finally {
    if (beams > 1) {
      beamHidden.dispose()
      beamMask.dispose()
    }
  }
}

const translations = []
for (const input of inputs) {
  const ids = intTensor([input])
  const mask = intTensor([input.map(() => 1)])
  const {last_hidden_state: hidden} = await encoder.run({input_ids: ids, attention_mask: mask})
  translations.push(await decode(input, hidden, mask))
  ids.dispose()
  mask.dispose()
  hidden.dispose()
}
await Promise.all([encoder.release(), decoder.release()])
process.stdout.write(JSON.stringify(translations))
