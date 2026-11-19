import os
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.git','__pycache__')]
    for f in files:
        p = os.path.join(root, f)
        print(p, os.path.getsize(p))
print('CWD', os.getcwd())
