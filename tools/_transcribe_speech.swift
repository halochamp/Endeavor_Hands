import Speech
import Foundation

func writeResult(text: String?, error: String?, to path: String) {
    let dict: [String: Any] = [
        "text": text ?? NSNull(),
        "error": error ?? NSNull(),
    ]
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: []) {
        try? data.write(to: URL(fileURLWithPath: path))
    }
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    exit(1)
}
let mediaPath = args[1]
let outPath = args[2]
let deadlineSec = args.count >= 4 ? (Double(args[3]) ?? 120) : 120
let url = URL(fileURLWithPath: mediaPath)

var finished = false
var resultText: String? = nil
var resultError: String? = nil

// AVFoundation (the same framework backing SFSpeechURLRecognitionRequest) demuxes
// the audio track internally for video containers (.mp4/.mov) — no separate
// extraction step needed; confirmed empirically against a real .mov file.
SFSpeechRecognizer.requestAuthorization { status in
    guard status == .authorized else {
        resultError = "not authorized: status=\(status.rawValue)"
        finished = true
        return
    }
    guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "th-TH")) else {
        resultError = "recognizer init failed"
        finished = true
        return
    }
    guard recognizer.isAvailable else {
        resultError = "recognizer not available"
        finished = true
        return
    }
    recognizer.defaultTaskHint = .dictation
    let request = SFSpeechURLRecognitionRequest(url: url)
    request.requiresOnDeviceRecognition = true
    request.shouldReportPartialResults = true
    _ = recognizer.recognitionTask(with: request) { result, error in
        if let error = error {
            resultError = error.localizedDescription
            finished = true
            return
        }
        if let result = result {
            resultText = result.bestTranscription.formattedString
            if result.isFinal {
                finished = true
            }
        }
    }
}

// Block the main thread with a polling RunLoop, NOT a DispatchSemaphore —
// recognitionTask's completion handler is delivered via the main run loop,
// which a synchronous semaphore-wait would never let drain (deadlock).
let deadline = Date().addingTimeInterval(deadlineSec)
while !finished && Date() < deadline {
    RunLoop.main.run(until: Date().addingTimeInterval(0.2))
}
if !finished {
    resultError = "timeout waiting for transcription (or for the one-time consent dialog to be granted)"
}
writeResult(text: resultText, error: resultError, to: outPath)
