// voicemic — microphone capture through Apple's voice-processing audio unit:
// echo cancellation (device playback removed from the mic — headphone-free
// Zoom/Meet), automatic gain control, and noise suppression.
// Used for ONLINE meetings; in-person keeps raw capture (calibration showed
// voice processing can gate distant far-field speech).
//
// Usage: voicemic <output.caf>   — runs until SIGINT/SIGTERM, then finalizes.

import AVFoundation
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: voicemic <output.caf>\n".data(using: .utf8)!)
    exit(2)
}
let outURL = URL(fileURLWithPath: args[1])

let engine = AVAudioEngine()
do {
    // must be enabled before formats are queried
    try engine.inputNode.setVoiceProcessingEnabled(true)
    // Voice processing makes macOS duck ALL other output, the same way a phone
    // call does — so during a recording the speakers went quiet even at full
    // volume, which is miserable when you are recording a call you also need
    // to hear. Ask for the minimum ducking: echo cancellation still works
    // (it uses the output as its reference), it just stops turning it down.
    if #available(macOS 14.0, *) {
        engine.inputNode.voiceProcessingOtherAudioDuckingConfiguration =
            AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                enableAdvancedDucking: false,
                duckingLevel: .min)
    }
} catch {
    FileHandle.standardError.write("voice processing unavailable: \(error)\n".data(using: .utf8)!)
    exit(3)
}

let fmt = engine.inputNode.outputFormat(forBus: 0)
guard fmt.sampleRate > 0 else {
    FileHandle.standardError.write("no input format — mic permission?\n".data(using: .utf8)!)
    exit(3)
}
// The VP unit outputs multi-channel (voice + reference channels). Write ONLY
// channel 0 (the processed voice) as mono — 9× smaller files and directly
// convertible by ffmpeg (auto-downmix chokes on the 9-channel layout).
guard let monoFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                  sampleRate: fmt.sampleRate, channels: 1,
                                  interleaved: false) else { exit(3) }
var file: AVAudioFile?
do {
    file = try AVAudioFile(forWriting: outURL, settings: monoFmt.settings,
                           commonFormat: .pcmFormatFloat32, interleaved: false)
} catch {
    FileHandle.standardError.write("cannot open output: \(error)\n".data(using: .utf8)!)
    exit(3)
}

engine.inputNode.installTap(onBus: 0, bufferSize: 4096, format: fmt) { buf, _ in
    guard let src = buf.floatChannelData,
          let mono = AVAudioPCMBuffer(pcmFormat: monoFmt,
                                      frameCapacity: buf.frameLength)
    else { return }
    mono.frameLength = buf.frameLength
    mono.floatChannelData![0].update(from: src[0], count: Int(buf.frameLength))
    try? file?.write(from: mono)
}

func finish(_ code: Int32) {
    engine.stop()
    file = nil  // finalize
    exit(code)
}
let sigint = DispatchSource.makeSignalSource(signal: SIGINT)
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
sigint.setEventHandler { finish(0) }
sigterm.setEventHandler { finish(0) }
sigint.resume()
sigterm.resume()

do {
    try engine.start()
    FileHandle.standardError.write("voicemic capturing \(Int(fmt.sampleRate)) Hz \(fmt.channelCount) ch (AEC+AGC+NS)\n".data(using: .utf8)!)
} catch {
    FileHandle.standardError.write("engine start failed: \(error)\n".data(using: .utf8)!)
    exit(3)
}
dispatchMain()
