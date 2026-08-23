#!/usr/bin/env python3
"""Headless tests for the Bayer/V4L2 camera support (no camera needed).

Background: the lab's Imaging Source DFK 37BUX250 offers ONLY Bayer
formats (RGGB8 / RG16). OpenCV's V4L2 backend has no SRGGB8 support and
refuses to open such a device at all, so webcam.py drives it through
v4l2-ctl instead. The parsing/decision logic is pure and tested here; the
streaming itself needs hardware (bench check).

Run: .venv/bin/python tests/test_webcam_bayer.py
"""
# Runnable from anywhere: put the repo root (one level up) on sys.path
# so the app modules import when this file is executed directly.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))

import webcam

# Real output captured from the DFK 37BUX250 on the bench (2026-07-20).
LIST_FORMATS = """ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'RGGB' (8-bit Bayer RGRG/GBGB)
	[1]: 'RG16' (16-bit Bayer RGRG/GBGB)
"""

LIST_FORMATS_EXT = """ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'RGGB' (8-bit Bayer RGRG/GBGB)
		Size: Discrete 640x480
			Interval: Discrete 0.003s (370.000 fps)
		Size: Discrete 1216x1024
			Interval: Discrete 0.006s (180.000 fps)
		Size: Discrete 1920x1080
			Interval: Discrete 0.006s (172.000 fps)
		Size: Discrete 2448x2048
			Interval: Discrete 0.013s (75.000 fps)
	[1]: 'RG16' (16-bit Bayer RGRG/GBGB)
		Size: Discrete 640x480
			Interval: Discrete 0.013s (75.000 fps)
"""

ORDINARY_WEBCAM = """	[0]: 'YUYV' (YUYV 4:2:2)
	[1]: 'MJPG' (Motion-JPEG, compressed)
"""


def test_parse_formats():
    assert webcam.parse_formats(LIST_FORMATS) == ['RGGB', 'RG16']
    assert webcam.parse_formats(ORDINARY_WEBCAM) == ['YUYV', 'MJPG']
    assert webcam.parse_formats('') == []


def test_parse_frame_sizes_is_per_format():
    rggb = webcam.parse_frame_sizes(LIST_FORMATS_EXT, 'RGGB')
    assert rggb == [(640, 480), (1216, 1024), (1920, 1080), (2448, 2048)], rggb
    # sizes must not bleed across format sections
    assert webcam.parse_frame_sizes(LIST_FORMATS_EXT, 'RG16') == [(640, 480)]
    assert webcam.parse_frame_sizes(LIST_FORMATS_EXT, 'YUYV') == []


def test_choose_bayer():
    # the real camera: Bayer only -> we must take over
    assert webcam.choose_bayer(['RGGB', 'RG16']) == 'RGGB'
    assert webcam.choose_bayer(['BGGR']) == 'BGGR'
    # an ordinary webcam -> leave it to OpenCV
    assert webcam.choose_bayer(['YUYV', 'MJPG']) is None
    # mixed: OpenCV can handle the normal format, so stay out of the way
    assert webcam.choose_bayer(['YUYV', 'RGGB']) is None
    assert webcam.choose_bayer([]) is None


def test_choose_size_prefers_largest_within_budget():
    sizes = [(640, 480), (1216, 1024), (1920, 1080), (2448, 2048)]
    assert webcam.choose_size(sizes) == (1920, 1080)
    # a sensor whose every mode is huge still yields something usable
    assert webcam.choose_size([(2448, 2048)]) == (2448, 2048)
    assert webcam.choose_size([(640, 480)]) == (640, 480)
    assert webcam.choose_size([]) is None


def test_bayer_code_table_maps_to_real_opencv_constants():
    # The V4L2 -> OpenCV mapping is deliberately shifted (V4L2 RGGB is
    # OpenCV's BayerBG); guard against someone "fixing" it to the naive
    # name and silently swapping red and blue.
    assert webcam.BAYER_CV_CODE['RGGB'] == 'COLOR_BayerBG2BGR'
    assert webcam.BAYER_CV_CODE['BGGR'] == 'COLOR_BayerRG2BGR'
    try:
        import cv2
    except ImportError:
        print('    (cv2 absent - skipped constant check)')
        return
    for code in webcam.BAYER_CV_CODE.values():
        assert hasattr(cv2, code), f"cv2 has no {code}"


def test_frame_bytes_matches_geometry():
    cam = webcam.V4L2BayerCamera('/dev/null', 'RGGB', width=1920, height=1080)
    assert cam.frame_bytes == 1920 * 1080      # 8-bit Bayer: 1 byte/pixel
    assert not cam.is_open



def test_parse_level_list():
    assert webcam.parse_level_list('0.2, 0.4, 0.6') == [0.2, 0.4, 0.6]
    assert webcam.parse_level_list('0.2 0.4 0.6') == [0.2, 0.4, 0.6]
    assert webcam.parse_level_list('1.9, 0.5\t2.3') == [1.9, 0.5, 2.3]
    assert webcam.parse_level_list('-1, 0, 1') == [-1.0, 0.0, 1.0]
    assert webcam.parse_level_list('') is None
    assert webcam.parse_level_list('   ') is None
    assert webcam.parse_level_list(None) is None
    for bad in ('1, two, 3', 'abc'):
        try:
            webcam.parse_level_list(bad)
            assert False, f"{bad!r} must raise"
        except ValueError:
            pass



def test_timed_delays_explicit_wins():
    # explicit list overrides the regular schedule, sorted + de-duped
    assert webcam.timed_delays(explicit='300, 150, 600, 150',
                               start=0, interval=60, count=99) == [150.0, 300.0, 600.0]
    assert webcam.timed_delays(explicit='0, 30, 90') == [0.0, 30.0, 90.0]


def test_timed_delays_regular_schedule():
    assert webcam.timed_delays(start=0, interval=60, count=4) == [0.0, 60.0, 120.0, 180.0]
    assert webcam.timed_delays(explicit='', start=150, interval=150, count=3) == [150.0, 300.0, 450.0]
    # a single shot needs no interval
    assert webcam.timed_delays(start=90, interval=0, count=1) == [90.0]


def test_timed_delays_rejects_bad_input():
    for kw in (dict(start=0, interval=0, count=0),      # zero shots
               dict(start=0, interval=0, count=5),      # >1 shot needs interval
               dict(explicit='-5, 10')):                 # negative delay
        try:
            webcam.timed_delays(**kw)
            assert False, f"{kw} must raise"
        except ValueError:
            pass

def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
