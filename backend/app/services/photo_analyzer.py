"""
Photo Analyzer — AI rating dari foto magnetic plug, filter cut, screen.

Input : gambar dari upload (JPEG/PNG)
Output: rating A/B/C + detail temuan + warna dominan + ferrous estimate

Metode:
  1. Deteksi partikel gelap (metallic debris) vs background cerah
  2. Analisa warna: silver/gray = besi, copper/gold = kuningan, black = soot
  3. Coverage area partikel → rating A (<5%), B (5-25%), C (>25%)
  4. Bonus: deteksi apakah ada chunky/coarse material (C rating langsung)
"""

import io
import math
import numpy as np
from PIL import Image, ImageFilter


def analyze_inspection_photo(image_bytes: bytes, sample_type: str = 'magnetic_plug') -> dict:
    """
    Analisa foto inspeksi dan kembalikan rating A/B/C.

    Args:
        image_bytes: raw bytes dari file gambar
        sample_type: 'magnetic_plug' | 'filter_cut' | 'screen'

    Returns dict:
        rating: 'A' | 'B' | 'C'
        confidence: 0.0–1.0
        particle_coverage_pct: persentase area yang tertutupi partikel
        dominant_colors: list warna dominan yang ditemukan
        ferrous_detected: bool
        copper_detected: bool
        findings: list string penjelasan
        recommendation: string
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception as e:
        return {'error': f'Gambar tidak dapat dibaca: {e}', 'rating': None}

    # Resize ke ukuran standar untuk konsistensi analisa
    img = img.resize((512, 512), Image.LANCZOS)
    arr = np.array(img, dtype=np.float32)

    R, G, B = arr[:,:,0], arr[:,:,1], arr[:,:,2]

    findings = []
    dominant_colors = []

    # ── 1. Deteksi partikel gelap (metallic/carbon) ───────────────────────
    # Partikel logam: pixel gelap (brightness < 80) di atas background terang
    brightness = (R + G + B) / 3.0
    dark_mask = brightness < 85
    total_pixels = 512 * 512

    # Background estimation: area paling terang (assumed = clean white/light background)
    bright_mask = brightness > 180
    bright_pct = bright_mask.sum() / total_pixels

    # Particle coverage
    particle_pct = dark_mask.sum() / total_pixels * 100

    # ── 2. Analisa warna partikel ─────────────────────────────────────────

    # Besi/baja: dark gray-silver → R≈G≈B, semua rendah
    metal_mask = dark_mask & (np.abs(R-G) < 25) & (np.abs(G-B) < 25)
    ferrous_pct = metal_mask.sum() / total_pixels * 100
    ferrous_detected = ferrous_pct > 0.5

    # Tembaga/kuningan: orange-copper hue → R tinggi, G sedang, B rendah
    copper_mask = (R > 120) & (R > G * 1.3) & (G > B * 1.2) & (brightness < 200) & (brightness > 60)
    copper_pct = copper_mask.sum() / total_pixels * 100
    copper_detected = copper_pct > 0.3

    # Silver/aluminum: bright metallic → R≈G≈B, semua medium-high
    silver_mask = (brightness > 100) & (brightness < 200) & (np.abs(R-G) < 20) & (np.abs(G-B) < 20)
    silver_pct = silver_mask.sum() / total_pixels * 100

    # Soot/carbon: very dark black
    soot_mask = brightness < 40
    soot_pct = soot_mask.sum() / total_pixels * 100

    # ── 3. Deteksi chunk/coarse material (partikel besar) ─────────────────
    # Gunakan erode/dilate untuk cari connected regions besar
    dark_bool = (brightness < 80).astype(np.uint8)
    chunky_detected = False

    # Simple connected component approximation: sum of large dark regions
    # Scan untuk patch > 15x15 pixel (≈chunk material)
    kernel_size = 15
    for row in range(0, 512-kernel_size, kernel_size):
        for col in range(0, 512-kernel_size, kernel_size):
            patch = dark_bool[row:row+kernel_size, col:col+kernel_size]
            if patch.sum() > (kernel_size * kernel_size * 0.7):  # 70% dark patch
                chunky_detected = True
                break
        if chunky_detected:
            break

    # ── 4. Color summary ──────────────────────────────────────────────────
    if ferrous_pct > 1.0: dominant_colors.append(f'Ferrous/besi ({ferrous_pct:.1f}%)')
    if copper_pct > 0.5:  dominant_colors.append(f'Copper/tembaga ({copper_pct:.1f}%)')
    if silver_pct > 2.0:  dominant_colors.append(f'Silver/aluminum ({silver_pct:.1f}%)')
    if soot_pct > 1.0:    dominant_colors.append(f'Soot/carbon ({soot_pct:.1f}%)')
    if not dominant_colors: dominant_colors = ['Tidak ada partikel signifikan terdeteksi']

    # ── 5. Rating logic ───────────────────────────────────────────────────
    #
    # Rating C langsung jika:
    #   - Ada chunk/coarse material besar
    #   - Particle coverage > 25%
    #   - Copper > 3% (bearing material = serious)
    #
    # Rating B jika:
    #   - Coverage 5–25%
    #   - Ferrous 2–8%
    #
    # Rating A jika:
    #   - Coverage < 5%

    if chunky_detected or particle_pct > 25 or copper_pct > 3.0:
        rating = 'C'
        confidence = 0.85 if chunky_detected else 0.75
        findings.append(f'Coverage partikel tinggi: {particle_pct:.1f}%')
        if chunky_detected:
            findings.append('Terdeteksi material kasar/chunk — kemungkinan material bearing')
        if copper_pct > 3.0:
            findings.append(f'Copper tinggi ({copper_pct:.1f}%) — indikasi keausan bearing/bushing serius')
        recommendation = 'STOP — LAPORKAN KE SUPERIOR SEGERA. Ambil sampel SOS tambahan dan inspeksi komponen.'

    elif particle_pct > 5 or ferrous_pct > 2.0 or copper_pct > 0.5:
        rating = 'B'
        confidence = 0.72
        findings.append(f'Coverage partikel sedang: {particle_pct:.1f}%')
        if ferrous_pct > 2.0:
            findings.append(f'Partikel ferrous terdeteksi ({ferrous_pct:.1f}%) — monitor keausan normal')
        if copper_pct > 0.5:
            findings.append(f'Jejak copper ({copper_pct:.1f}%) — perhatikan bearing/bushing')
        recommendation = 'Monitor — ambil SOS di interval lebih pendek. Perhatikan tren.'

    else:
        rating = 'A'
        confidence = 0.80
        findings.append(f'Coverage partikel rendah: {particle_pct:.1f}%')
        findings.append('Kondisi normal — tidak ada material kasar terdeteksi')
        recommendation = 'Normal — lanjutkan interval service biasa.'

    # Tambahkan info sample type spesifik
    if sample_type == 'magnetic_plug':
        if ferrous_detected:
            findings.append('Magnetic plug: partikel ferrous menempel — sesuai fungsi filter magnet')
    elif sample_type == 'filter_cut':
        findings.append('Filter cut: perhatikan distribusi partikel pada media filter')
    elif sample_type == 'screen':
        findings.append('Screen: cek apakah ada penyumbatan pada mesh')

    # Confidence adjustment berdasarkan kualitas gambar
    if bright_pct < 0.2:
        confidence *= 0.8
        findings.append('⚠ Gambar terlalu gelap — hasil analisa mungkin kurang akurat')
    elif bright_pct > 0.9:
        confidence *= 0.85
        findings.append('⚠ Gambar terlalu terang/overexposed')

    return {
        'rating': rating,
        'confidence': round(confidence, 2),
        'particle_coverage_pct': round(float(particle_pct), 2),
        'ferrous_pct': round(float(ferrous_pct), 2),
        'copper_pct': round(float(copper_pct), 2),
        'soot_pct': round(float(soot_pct), 2),
        'dominant_colors': dominant_colors,
        'ferrous_detected': bool(ferrous_detected),
        'copper_detected': bool(copper_detected),
        'chunky_material': bool(chunky_detected),
        'findings': findings,
        'recommendation': recommendation,
        'sample_type': sample_type,
    }
