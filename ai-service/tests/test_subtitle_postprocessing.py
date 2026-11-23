from app.media import clean_cues


def test_clean_cues_removes_repeated_echo():
    cues = clean_cues([
        (0.0, 2.0, 'Xin chào các bạn xin chào các bạn xin chào các bạn'),
        (2.0, 4.0, 'Xin chào các bạn xin chào các bạn xin chào các bạn'),
    ])
    assert len(cues) == 1


def test_clean_cues_splits_long_cue():
    text = ' '.join([f'Từ{i}' for i in range(30)])
    cues = clean_cues([(0.0, 10.0, text)])
    assert len(cues) > 1
    assert all(end - start <= 7.1 for start, end, _ in cues)


def test_clean_cues_removes_overlapping_duplicate():
    cues = clean_cues([
        (0.0, 3.0, 'Nội dung giống nhau'),
        (2.8, 5.0, 'Nội dung giống nhau'),
    ])
    assert len(cues) == 1
    assert cues[0][0] == 0.0
