// systemaudio — capture macOS system audio (all apps: Zoom, Meet in browser, etc.)
// to an audio file using ScreenCaptureKit. No bot joins the meeting.
//
// Usage: systemaudio <output.caf> [max_seconds]
// Runs until SIGINT/SIGTERM (or max_seconds), then finalizes the file and exits 0.
// Requires the Screen Recording permission (macOS TCC) for the launching app/terminal.

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: systemaudio <output.caf> [max_seconds]\n".data(using: .utf8)!)
    exit(2)
}
let outputURL = URL(fileURLWithPath: args[1])
let maxSeconds = args.count >= 3 ? Double(args[2]) ?? 0 : 0

final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate {
    var file: AVAudioFile?
    var frames: Int64 = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let desc = sampleBuffer.formatDescription,
              var asbd = desc.audioStreamBasicDescription else { return }
        do {
            let format = AVAudioFormat(streamDescription: &asbd)!
            if file == nil {
                file = try AVAudioFile(forWriting: outputURL, settings: format.settings,
                                       commonFormat: .pcmFormatFloat32,
                                       interleaved: format.isInterleaved)
                FileHandle.standardError.write("recording \(Int(asbd.mSampleRate)) Hz \(asbd.mChannelsPerFrame) ch\n".data(using: .utf8)!)
            }
            try sampleBuffer.withAudioBufferList { audioBufferList, _ in
                guard let pcm = AVAudioPCMBuffer(pcmFormat: file!.processingFormat,
                                                 bufferListNoCopy: audioBufferList.unsafePointer)
                else { return }
                try file?.write(from: pcm)
                frames += Int64(pcm.frameLength)
            }
        } catch {
            FileHandle.standardError.write("write error: \(error)\n".data(using: .utf8)!)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("stream stopped: \(error)\n".data(using: .utf8)!)
        finishAndExit(code: 1)
    }
}

let sink = AudioSink()
var scStream: SCStream?
let done = DispatchSemaphore(value: 0)

func finishAndExit(code: Int32) {
    scStream?.stopCapture { _ in
        sink.file = nil  // finalize
        FileHandle.standardError.write("finalized \(sink.frames) frames\n".data(using: .utf8)!)
        exit(code)
    }
    // Fallback if stopCapture callback never fires
    DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
        sink.file = nil
        exit(code)
    }
}

let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT)
let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
sigintSrc.setEventHandler { finishAndExit(code: 0) }
sigtermSrc.setEventHandler { finishAndExit(code: 0) }
sigintSrc.resume()
sigtermSrc.resume()

SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
    guard let content, let display = content.displays.first, error == nil else {
        FileHandle.standardError.write("ERROR: no shareable content — grant Screen Recording permission in System Settings > Privacy & Security (\(String(describing: error)))\n".data(using: .utf8)!)
        exit(3)
    }
    let filter = SCContentFilter(display: display, excludingWindows: [])
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true
    config.sampleRate = 48000
    config.channelCount = 1
    // minimal video (required by SCStream); we ignore the frames
    config.width = 2
    config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    let stream = SCStream(filter: filter, configuration: config, delegate: sink)
    scStream = stream
    do {
        try stream.addStreamOutput(sink, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "audio"))
        stream.startCapture { err in
            if let err {
                FileHandle.standardError.write("ERROR: startCapture failed: \(err)\n".data(using: .utf8)!)
                exit(3)
            }
            FileHandle.standardError.write("capture started\n".data(using: .utf8)!)
            if maxSeconds > 0 {
                DispatchQueue.global().asyncAfter(deadline: .now() + maxSeconds) {
                    finishAndExit(code: 0)
                }
            }
        }
    } catch {
        FileHandle.standardError.write("ERROR: addStreamOutput: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
}

done.wait()
