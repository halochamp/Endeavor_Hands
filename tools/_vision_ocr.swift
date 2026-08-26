import Foundation
import Vision
import AppKit

let args = CommandLine.arguments

// --langs : print supported recognition languages (accurate level)
if args.count > 1 && args[1] == "--langs" {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    if let langs = try? req.supportedRecognitionLanguages() {
        print(langs.joined(separator: ", "))
    }
    exit(0)
}

guard args.count > 1,
      let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("ERR: cannot load \(args.count > 1 ? args[1] : "<no path>")")
    exit(1)
}

// Output: one observation per line as `x\ty\tw\th\ttext` (boundingBox is
// normalized [0,1], origin BOTTOM-LEFT). The Python side strips coords for
// plain-text callers and uses them to reconstruct table layout.
let req = VNRecognizeTextRequest { (request, _) in
    guard let obs = request.results as? [VNRecognizedTextObservation] else { return }
    var out: [String] = []
    for o in obs {
        guard let s = o.topCandidates(1).first?.string else { continue }
        let b = o.boundingBox
        out.append(String(format: "%.5f\t%.5f\t%.5f\t%.5f\t%@",
                          b.origin.x, b.origin.y, b.size.width, b.size.height, s))
    }
    print(out.joined(separator: "\n"))
}
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = ["th-TH", "en-US"]

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do { try handler.perform([req]) } catch { print("ERR perform: \(error)") }
