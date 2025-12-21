fpath = 'dark_mode.svg'
with open(fpath,'rb') as f:
    lines = f.read().splitlines()
line_no = 68
if line_no-1 < len(lines):
    b = lines[line_no-1]
    print('BYTES:', b)
    print('REPR:', repr(b))
    print('HEX:', ' '.join(f"{c:02x}" for c in b))
else:
    print('File has', len(lines), 'lines')
