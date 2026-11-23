class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buffer = new ArrayBuffer(4096 * 2)
    this.position = 0
    this.enabled = false
    this.port.onmessage = ({data}) => {
      if (data === 'start') this.enabled = true
      if (data === 'flush') {
        this.enabled = false
        const aligned = this.position - this.position % 32
        if (aligned) {
          const tail = this.buffer.slice(0, aligned)
          this.port.postMessage(tail, [tail])
        }
        this.position = 0
        this.port.postMessage('flushed')
      }
    }
  }

  process(inputs) {
    const samples = inputs[0]?.[0]
    if (samples && this.enabled) {
      let frame = new DataView(this.buffer)
      for (const sample of samples) {
        const value = Math.max(-1, Math.min(1, sample))
        frame.setInt16(this.position, value < 0 ? value * 32768 : value * 32767, true)
        this.position += 2
        if (this.position === this.buffer.byteLength) {
          this.port.postMessage(this.buffer, [this.buffer])
          this.buffer = new ArrayBuffer(4096 * 2)
          frame = new DataView(this.buffer)
          this.position = 0
        }
      }
    }
    return true
  }
}

registerProcessor('pcm-capture', PcmCapture)
