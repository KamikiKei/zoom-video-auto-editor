#!/usr/bin/env python3
"""
話者分離 (簡易ダイアライゼーション) — 運営 / 登壇者の聞き分け用

やること:
  1. 動画から音声を 16kHz mono wav で抽出
  2. 発話区間を検出 (VAD: エネルギーベース)
  3. 各ウィンドウの MFCC 特徴をクラスタリングして話者A/B/... に分ける
     (numpy + scipy のみ。重いモデルは使わない)
  4. 区間リスト + 話者ごとの試聴サンプル (各話者の最初の明瞭な区間) を出力
  5. 「本編の話者 = 合計発話時間が最長の話者」とみなし、その話者の
     最初の長い区間の開始時刻をトリミング開始の候補として提案する

限界:
  - 声質が近い話者同士は混ざることがある。最終判断は試聴サンプルで人が行う前提。
  - 正確な名前は分からない (話者0/1としか言えない)。誰が運営かは
    試聴してラベル付けする。Zoomの参加者別音声や字幕(VTT)がある場合はそちらが確実。

使い方:
  python3 diarize.py input.mp4 -o segments.json --samples-dir samples/
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.fft import dct


# ---------- 音声読み込み ----------
def extract_wav(video: Path, wav_path: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
        check=True,
    )


# ---------- MFCC ----------
def mel_filterbank(sr, n_fft, n_mels=26, fmin=50, fmax=None):
    fmax = fmax or sr / 2
    mel = lambda f: 2595 * np.log10(1 + f / 700)
    inv = lambda m: 700 * (10 ** (m / 2595) - 1)
    pts = inv(np.linspace(mel(fmin), mel(fmax), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(1, n_mels + 1):
        left, center, right = bins[i - 1], bins[i], bins[i + 1]
        for k in range(left, center):
            if center > left:
                fb[i - 1, k] = (k - left) / (center - left)
        for k in range(center, right):
            if right > center:
                fb[i - 1, k] = (right - k) / (right - center)
    return fb


def mfcc_features(signal, sr, win=1.0, hop=0.5):
    """win秒ウィンドウごとに (時刻, MFCC平均+標準偏差, RMS) を返す"""
    n_fft = 512
    frame_len = int(sr * 0.025)
    frame_hop = int(sr * 0.010)
    fb = mel_filterbank(sr, n_fft)
    window_samples = int(sr * win)
    hop_samples = int(sr * hop)
    feats, times, rms_list = [], [], []
    x = signal.astype(np.float64)
    if np.max(np.abs(x)) > 0:
        x = x / np.max(np.abs(x))
    for start in range(0, max(1, len(x) - window_samples + 1), hop_samples):
        seg = x[start:start + window_samples]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        # フレーム分割してMFCCを平均
        mfccs = []
        for fs in range(0, len(seg) - frame_len + 1, frame_hop):
            frame = seg[fs:fs + frame_len] * np.hanning(frame_len)
            spec = np.abs(np.fft.rfft(frame, n=n_fft)) ** 2
            mel_e = fb @ spec + 1e-10
            cep = dct(np.log(mel_e), type=2, norm="ortho")[:13]
            mfccs.append(cep)
        if not mfccs:
            continue
        m = np.array(mfccs)
        # 平均のみ使う。標準偏差まで入れると「2話者が混ざった境界ウィンドウ」が
        # 外れ値クラスタになって本筋の2人が同クラスタに潰れるため。
        feats.append(m.mean(axis=0))
        # ピッチ推定 (自己相関) も特徴に足す: 声の高さは話者の強い手がかり
        f0s = []
        for fs in range(0, len(seg) - frame_len + 1, frame_hop * 4):
            frame = seg[fs:fs + frame_len]
            if np.sqrt(np.mean(frame ** 2)) < 0.02:
                continue
            ac = np.correlate(frame, frame, mode="full")[frame_len - 1:]
            lo, hi = int(sr / 400), int(sr / 60)  # 60〜400Hz
            if hi < len(ac):
                lag = lo + int(np.argmax(ac[lo:hi]))
                if ac[lag] > 0.3 * ac[0]:
                    f0s.append(sr / lag)
        feats[-1] = np.append(feats[-1], np.median(f0s) if f0s else 0.0)
        times.append(start / sr)
        rms_list.append(rms)
    return np.array(times), np.array(feats), np.array(rms_list)


# ---------- k-means (numpy実装) ----------
def kmeans(X, k, seed=0, iters=100):
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(X), size=k, replace=False)
    C = X[idx].copy()
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        d = ((X[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        new = d.argmin(axis=1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            if (labels == j).any():
                C[j] = X[labels == j].mean(axis=0)
    return labels, C


def smooth(labels, size=3):
    out = labels.copy()
    half = size // 2
    for i in range(len(labels)):
        lo, hi = max(0, i - half), min(len(labels), i + half + 1)
        vals, counts = np.unique(labels[lo:hi], return_counts=True)
        out[i] = vals[counts.argmax()]
    return out


def diarize(video: Path, n_speakers=2, min_segment=1.5):
    tmp_wav = Path("/tmp/_diarize.wav")
    extract_wav(video, tmp_wav)
    sr, data = wavfile.read(tmp_wav)
    if data.dtype == np.int16:
        signal = data.astype(np.float64) / 32768.0
    else:
        signal = data.astype(np.float64)

    times, feats, rms = mfcc_features(signal, sr)
    if len(times) == 0:
        raise RuntimeError("音声が短すぎます")

    # VAD: 発話が大半を占める録音でも壊れないよう、上位RMS基準の相対閾値
    thr = max(0.02, 0.12 * np.percentile(rms, 95))
    voiced = rms > thr
    if voiced.sum() < n_speakers * 3:
        raise RuntimeError("発話区間が少なすぎて話者を分けられません")

    X = feats[voiced]
    # 標準化 (エネルギー成分 c0 は声の大きさに引っ張られるので弱める)
    # 外れ値 (境界ウィンドウ等) がクラスタを潰さないよう z を ±4 でクリップ
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    Xn = np.clip((X - mu) / sd, -4, 4)
    Xn[:, 0] *= 0.3

    best = None
    for seed in range(5):
        labels, C = kmeans(Xn, n_speakers, seed=seed)
        inertia = float(((Xn - C[labels]) ** 2).sum())
        if best is None or inertia < best[0]:
            best = (inertia, labels)
    labels = smooth(best[1], size=3)

    vtimes = times[voiced]
    win = 1.0
    # 区間化: 連続した同ラベルをまとめる
    segments = []
    cur_label, cur_start, cur_end = None, None, None
    for t, lab in zip(vtimes, labels):
        if lab != cur_label:
            if cur_label is not None:
                segments.append([cur_start, cur_end, int(cur_label)])
            cur_label, cur_start = lab, t
        cur_end = t + win
    if cur_label is not None:
        segments.append([cur_start, cur_end, int(cur_label)])

    # 短い区間を隣に吸収
    merged = []
    for s in segments:
        if merged and (s[1] - s[0]) < min_segment and s[2] != merged[-1][2]:
            merged[-1][1] = s[1]
        elif merged and s[2] == merged[-1][2]:
            merged[-1][1] = s[1]
        else:
            merged.append(list(s))
    # 同ラベル隣接を再結合
    final = []
    for s in merged:
        if final and s[2] == final[-1][2]:
            final[-1][1] = s[1]
        else:
            final.append(s)

    # 話者ごとの合計時間
    totals = {}
    for a, b, lab in final:
        totals[lab] = totals.get(lab, 0) + (b - a)
    main_speaker = max(totals, key=totals.get)

    # 提案: 本編話者の最初の「5秒以上続く」区間の開始
    suggestion = None
    for a, b, lab in final:
        if lab == main_speaker and (b - a) >= 5:
            suggestion = a
            break
    if suggestion is None:
        for a, b, lab in final:
            if lab == main_speaker:
                suggestion = a
                break

    return {
        "speakers": {str(k): round(v, 1) for k, v in sorted(totals.items())},
        "main_speaker_guess": int(main_speaker),
        "suggested_trim_start": round(float(suggestion), 1) if suggestion is not None else None,
        "segments": [
            {"start": round(a, 1), "end": round(b, 1), "speaker": int(lab)}
            for a, b, lab in final
        ],
    }


def export_samples(video: Path, result: dict, samples_dir: Path):
    samples_dir.mkdir(parents=True, exist_ok=True)
    by_speaker = {}
    for seg in result["segments"]:
        sp = seg["speaker"]
        dur = seg["end"] - seg["start"]
        if sp not in by_speaker or dur > by_speaker[sp][1]:
            by_speaker[sp] = (seg, dur)
    paths = {}
    for sp, (seg, _) in by_speaker.items():
        out = samples_dir / f"speaker{sp}.mp3"
        length = min(8.0, seg["end"] - seg["start"])
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(seg["start"]),
             "-i", str(video), "-t", str(length), "-vn",
             "-c:a", "libmp3lame", "-q:a", "4", str(out)],
            check=True,
        )
        paths[str(sp)] = str(out)
    return paths


def main():
    ap = argparse.ArgumentParser(description="話者分離 (運営/登壇者の聞き分け)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="segments.json")
    ap.add_argument("--speakers", type=int, default=2, help="話者数 (既定2)")
    ap.add_argument("--samples-dir", help="話者ごとの試聴サンプル出力先")
    args = ap.parse_args()

    video = Path(args.input)
    result = diarize(video, n_speakers=args.speakers)
    if args.samples_dir:
        result["samples"] = export_samples(video, result, Path(args.samples_dir))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"話者別 発話時間: {result['speakers']}")
    print(f"本編の話者(推定): 話者{result['main_speaker_guess']}")
    print(f"トリミング開始の提案: {result['suggested_trim_start']} 秒")
    print(f"区間数: {len(result['segments'])} -> {args.output}")


if __name__ == "__main__":
    main()
