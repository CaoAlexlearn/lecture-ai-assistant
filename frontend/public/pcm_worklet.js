class PCMForwarder extends AudioWorkletProcessor {
  constructor() {
    super()
    this.sequence = 0
    this.stopped = false
    this.port.onmessage = (event) => {
      if (event.data?.command !== "stop" || this.stopped) return
      this.stopped = true
      // MessagePort ordering guarantees every earlier PCM message reaches the
      // main thread before this acknowledgement.
      this.port.postMessage({ type: "stopped", lastSequence: this.sequence })
    }
  }

  process(inputs) {
    if (this.stopped) return true
    const input = inputs[0]
    if (input && input[0] && input[0].length) {
      const copy = new Float32Array(input[0].length)
      copy.set(input[0])
      this.sequence += 1
      this.port.postMessage(
        { type: "pcm", sequence: this.sequence, buffer: copy.buffer },
        [copy.buffer],
      )
    }
    return true
  }
}

registerProcessor("pcm-forwarder", PCMForwarder)
