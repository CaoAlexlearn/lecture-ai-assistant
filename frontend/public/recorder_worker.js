let sampleRate = 48000
let targetSampleRate = 16000
let buffered = new Float32Array(0)
let bufferStartIndex = 0
let totalInputSamples = 0
let nextOutputIndex = 0

function reset(config) {
  sampleRate = Number(config.sampleRate) || 48000
  targetSampleRate = Number(config.targetSampleRate) || 16000
  buffered = new Float32Array(0)
  bufferStartIndex = 0
  totalInputSamples = 0
  nextOutputIndex = 0
}

function append(input) {
  if (!input.length) return
  const combined = new Float32Array(buffered.length + input.length)
  combined.set(buffered)
  combined.set(input, buffered.length)
  buffered = combined
  totalInputSamples += input.length
}

function resample(flush = false) {
  const output = []
  // Keep interpolation state across AudioWorklet's 128-frame messages. This
  // prevents the per-message rounding drift of the previous implementation.
  while (true) {
    // Keep the sample clocks as integers. Even 44.1 kHz input can therefore
    // run for hours without floating-point phase or sample-count drift.
    const sourceNumerator = nextOutputIndex * sampleRate
    const absoluteLeftIndex = Math.floor(sourceNumerator / targetSampleRate)
    const hasSource = flush
      ? absoluteLeftIndex < totalInputSamples
      : absoluteLeftIndex + 1 < totalInputSamples
    if (!hasSource) break
    const leftIndex = absoluteLeftIndex - bufferStartIndex
    const rightIndex = Math.min(leftIndex + 1, buffered.length - 1)
    const fraction = (sourceNumerator % targetSampleRate) / targetSampleRate
    output.push(buffered[leftIndex] * (1 - fraction) + buffered[rightIndex] * fraction)
    nextOutputIndex += 1
  }

  const nextAbsoluteIndex = Math.floor(
    (nextOutputIndex * sampleRate) / targetSampleRate,
  )
  const consumed = Math.min(
    Math.max(nextAbsoluteIndex - bufferStartIndex, 0),
    buffered.length,
  )
  if (consumed > 0) {
    buffered = buffered.slice(consumed)
    bufferStartIndex += consumed
  }
  if (flush) {
    buffered = new Float32Array(0)
    bufferStartIndex = totalInputSamples
  }
  return output
}

function emitPcm(samples) {
  if (!samples.length) return
  const pcm = new ArrayBuffer(samples.length * 2)
  const view = new DataView(pcm)
  samples.forEach((sample, index) => {
    const value = Math.max(-1, Math.min(1, sample))
    view.setInt16(index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true)
  })
  self.postMessage({ type: "pcm", buffer: pcm }, [pcm])
}

self.onmessage = (event) => {
  const { command } = event.data
  if (command === "init") {
    reset(event.data.config || {})
    return
  }
  if (command === "record") {
    append(new Float32Array(event.data.buffer))
    emitPcm(resample(false))
    return
  }
  if (command === "flush") {
    emitPcm(resample(true))
    self.postMessage({ type: "flushed", requestId: event.data.requestId })
  }
}
