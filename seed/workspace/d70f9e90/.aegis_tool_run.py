p='logs/nginx_access_20261118.log'
lines=[l for l in open(p,encoding='utf-8').read().split('\n') if l.strip()]
print(len(lines))
for i,l in enumerate(lines[24:],24):
    print(i,repr(l))
