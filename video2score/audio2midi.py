"""
音声抽出と MIDI 変換モジュール。

movie2midi プロジェクトの実装を元に、video2score パイプラインに統合。
"""
import subprocess
import tempfile
from pathlib import Path


def extract_audio(video_path: str, audio_path: str, sr: int = 44100) -> None:
    """ffmpeg で動画から音声を WAV (mono) として抽出する。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-f", "wav", str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg による音声抽出に失敗: {result.stderr.decode()[:300]}"
        )


def transcribe_basic_pitch(
    audio_path: str,
    output_path: str,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length: float = 58.0,
) -> None:
    """Spotify basic-pitch モデルで音声を MIDI に変換する（汎用・多楽器対応）。"""
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH

    audio_path = Path(audio_path)
    output_path = Path(output_path)

    _, midi_data, _ = predict(
        str(audio_path),
        model_or_model_path=ICASSP_2022_MODEL_PATH,
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
        minimum_note_length=min_note_length,
    )
    midi_data.write(str(output_path))


def _piano_compat_patch() -> None:
    """piano_transcription_inference が librosa >= 0.10 で動作するための互換パッチ。

    librosa 0.10 以降で librosa.core.audio サブモジュールが削除されたため、
    piano_transcription_inference が使う旧パスの関数を再現する:
      - librosa.core.audio.util.buf_to_float
      - librosa.core.audio.resample
    """
    import sys
    import types
    import numpy as np

    if "librosa.core.audio" in sys.modules:
        return

    def _buf_to_float(x, n_bytes: int = 2, dtype=np.float32):
        scale = 1.0 / float(1 << (8 * n_bytes - 1))
        return (scale * np.frombuffer(x, f"<i{n_bytes}")).astype(dtype)

    try:
        import librosa
        import librosa.core
    except ImportError:
        return

    def _resample(y, orig_sr, target_sr, res_type="kaiser_best",
                  fix=True, scale=False, **kwargs):
        if orig_sr == target_sr:
            return y
        # librosa.resample を呼ぶと再帰ループになるため resampy/scipy を直接使う
        try:
            import resampy
            return resampy.resample(y, orig_sr, target_sr).astype(y.dtype)
        except ImportError:
            pass
        import scipy.signal
        n = int(round(y.shape[-1] * target_sr / orig_sr))
        return scipy.signal.resample(y, n, axis=-1).astype(y.dtype)

    audio_mod = types.ModuleType("librosa.core.audio")
    audio_mod.util = types.SimpleNamespace(buf_to_float=_buf_to_float)
    audio_mod.resample = _resample
    sys.modules["librosa.core.audio"] = audio_mod
    librosa.core.__dict__["audio"] = audio_mod


def transcribe_piano(audio_path: str, output_path: str) -> None:
    """ピアノ豜音特化モデルで音声を MIDI に変換する（ピアノ専用・高精度）。

    使用には `pip install video2score[piano]` が必要。
    """
    try:
        import torch
        _piano_compat_patch()
        from piano_transcription_inference import PianoTranscription, sample_rate, load_audio
    except ImportError:
        raise ImportError(
            "piano モデルを使うには: pip install video2score[piano]\n"
            "または: pip install torch piano-transcription-inference"
        )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    transcriptor = PianoTranscription(device=device)
    audio, _ = load_audio(str(audio_path), sr=sample_rate, mono=True)
    transcriptor.transcribe(audio, str(output_path))


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="動画ファイルのピアノ演奏を MIDI に変換する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", help="入力動画ファイル")
    parser.add_argument("-o", "--output", default=None, help="出力 MIDI パス")
    parser.add_argument(
        "--model",
        choices=["basic-pitch", "piano"],
        default="basic-pitch",
        help="変換モデル: basic-pitch (汎用) / piano (ピアノ専用・要 torch)",
    )
    parser.add_argument("--onset-threshold", type=float, default=0.5)
    parser.add_argument("--frame-threshold", type=float, default=0.3)
    parser.add_argument("--min-note-length", type=float, default=58.0)
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"エラー: ファイルが見つかりません: {video_path}", file=sys.stderr)
        raise SystemExit(1)

    output_path = Path(args.output) if args.output else video_path.with_suffix(".mid")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / "audio.wav"
        print(f"[1/2] 音声抽出中: {video_path.name}")
        extract_audio(str(video_path), str(audio_path))

        print(f"[2/2] MIDI 変換中 (model={args.model})...")
        if args.model == "basic-pitch":
            transcribe_basic_pitch(
                str(audio_path), str(output_path),
                onset_threshold=args.onset_threshold,
                frame_threshold=args.frame_threshold,
                min_note_length=args.min_note_length,
            )
        else:
            transcribe_piano(str(audio_path), str(output_path))

    print(f"完了: {output_path}")


if __name__ == "__main__":
    main()
