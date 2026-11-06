import pathlib

p = pathlib.Path("src/shrinkray/engine/search.py")
s = p.read_text()

# find_optimal_crf_and_skip: the parameter + the forward
old = """    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,"""
assert s.count(old) == 1, f"expected one remaining signature, found {s.count(old)}"
new = """    target_vmaf: float = DEFAULT_TARGET_VMAF,
    crf_range: tuple[int, int] = DEFAULT_CRF_RANGE,
    vmaf_margin: float = 0.0,"""
s = s.replace(old, new, 1)

old2 = """        target_vmaf=target_vmaf,
        crf_range=crf_range,
        sample_count=sample_count,"""
assert old2 in s
new2 = """        target_vmaf=target_vmaf,
        crf_range=crf_range,
        vmaf_margin=vmaf_margin,
        sample_count=sample_count,"""
s = s.replace(old2, new2, 1)
p.write_text(s)
print("find_optimal_crf_and_skip patched")
