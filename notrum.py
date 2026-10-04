#!/usr/bin/env python3
"""
NOTRUM - team trick-taking card game with bidding, challenge & rechallenge.
Run:  python3 notrum.py      then everyone opens  http://<host-ip>:8000
No external libraries needed (Python 3.8+).
"""
import json, random, string, threading, socket, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOCK = threading.Lock()
ROOMS = {}
SUITS = "SHDC"
STR = {'N': 5, 'S': 4, 'H': 3, 'D': 2, 'C': 1}      # bid hierarchy: No-trump > S > H > D > C
RN = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}
rname = lambda r: RN.get(r, str(r))


def log(room, msg):
    room['log'].append(msg)
    room['log'] = room['log'][-40:]


def new_room(n):
    code = ''.join(random.choices(string.ascii_uppercase, k=4))
    while code in ROOMS:
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
    room = dict(code=code, n=n, players=[], phase='lobby', dealer=n - 1, scores=[0, 0], log=[],
                removed=[], hands=[], trick=[], last=None, turn=0, cur=None, bidder=None, passed=set(),
                mult=1, tw=[0, 0], played=0, pending=set(), challenger=None, rechal=False,
                result='', k=0, trump=None, bids={}, calls=[], acted=set())
    room['ts'] = time.time()
    ROOMS[code] = room
    return room


def deal(room):
    n = room['n']
    decks = 1 if n <= 6 else 2
    ranks = list(range(2, 15))
    removed = []
    while (len(ranks) * 4 * decks) % n:          # drop the lowest face value entirely (all copies)
        removed.append(ranks.pop(0))
    room['removed'] = [rname(r) for r in removed]
    deck = [{'r': r, 's': s} for r in ranks for s in SUITS] * decks
    random.shuffle(deck)
    k = len(deck) // n
    room['k'] = k
    room['hands'] = [sorted(deck[i * k:(i + 1) * k], key=lambda c: (c['s'], -c['r'])) for i in range(n)]


def new_hand(room):
    room['dealer'] = (room['dealer'] + 1) % room['n']
    deal(room)
    room.update(phase='bidding', turn=(room['dealer'] + 1) % room['n'], cur=None, bidder=None,
                passed=set(), mult=1, tw=[0, 0], played=0, pending=set(), challenger=None,
                rechal=False, trick=[], last=None, result='', trump=None, bids={}, calls=[], acted=set())
    log(room, "New hand dealt. %d cards each. Bidding starts with %s." %
        (room['k'], room['players'][room['turn']]['name']))


def support_suit(room, s):
    """Suit of the latest call made by a TEAMMATE of seat s (ongoing or earlier), else None."""
    for cs, n, su in reversed(room['calls']):
        if cs % 2 == s % 2 and cs != s:
            return su
    return None


def can_act(room, s):
    if s == room['bidder']:
        return False
    if s not in room['passed']:
        return True
    su = support_suit(room, s)             # passed players may only support a teammate's call
    if not su or not room['cur']:
        return False
    return (room['cur'][0] - STR[su]) // 10 + 1 <= room['k']


def do_bid(room, seat, d):
    if room['phase'] != 'bidding' or room['turn'] != seat:
        raise ValueError("Not your turn to bid")
    name = room['players'][seat]['name']
    was_passed = seat in room['passed']
    if d.get('pass'):
        if was_passed:
            log(room, "%s stays out." % name)
        else:
            room['passed'].add(seat)
            room['bids'][seat] = 'P'
            log(room, "%s passes." % name)
        room['acted'].add(seat)
    else:
        n, s = int(d['n']), d['suit']
        if s not in STR or not 1 <= n <= room['k']:
            raise ValueError("Invalid bid")
        if was_passed:
            su = support_suit(room, seat)
            if not su:
                raise ValueError("You passed. You can only support a call made by your teammate")
            if s != su:
                raise ValueError("You can only support your teammate's call in %s" % su)
        v = n * 10 + STR[s]
        if room['cur'] and v <= room['cur'][0]:
            raise ValueError("Bid must be higher: raise the number, or keep the number with a higher suit")
        room['cur'] = (v, n, s)
        room['bidder'] = seat
        room['bids'][seat] = [n, s]
        room['calls'].append((seat, n, s))
        room['acted'] = {seat}
        log(room, "%s %s %d %s." % (name, 'supports the team with' if was_passed else 'bids', n,
                                    'No Trump' if s == 'N' else s))
    N = room['n']
    if not room['cur'] and len(room['passed']) == N:
        log(room, "Everyone passed - redealing.")
        new_hand(room)
    elif room['cur'] and all(x in room['acted'] for x in range(N) if can_act(room, x)):
        b = room['bidder']
        room['trump'] = room['cur'][2]
        room['pending'] = {i for i in range(N) if i % 2 != b % 2}
        room['phase'] = 'challenge'
        room['turn'] = b
        log(room, "%s wins the bid: %d %s. Opponents may challenge." %
            (room['players'][b]['name'], room['cur'][1], room['trump']))
    else:
        t = seat
        while True:
            t = (t + 1) % N
            if can_act(room, t) and t not in room['acted']:
                break
        room['turn'] = t


def do_play(room, seat, d):
    if room['phase'] != 'play' or room['turn'] != seat:
        raise ValueError("Not your turn")
    card = {'r': int(d['r']), 's': d['s']}
    hand = room['hands'][seat]
    if card not in hand:
        raise ValueError("You don't hold that card")
    if room['trick']:
        led = room['trick'][0]['s']
        if card['s'] != led and any(c['s'] == led for c in hand):
            raise ValueError("You must follow suit (%s)" % led)
    hand.remove(card)
    if not room['trick']:
        room['last'] = None
    room['trick'].append({'seat': seat, **card})
    if len(room['trick']) < room['n']:
        room['turn'] = (seat + 1) % room['n']
        return
    t = room['trick']
    led, trump = t[0]['s'], room['trump']
    key = lambda c: (2 if c['s'] == trump else 1 if c['s'] == led else 0, c['r'])
    best = t[0]
    for c in t[1:]:
        if key(c) > key(best):
            best = c
    w = best['seat']
    room['tw'][w % 2] += 1
    room['played'] += 1
    room['last'] = dict(cards=t, winner=w)
    room['trick'] = []
    room['turn'] = w
    log(room, "%s wins the trick." % room['players'][w]['name'])
    if room['played'] == room['k']:
        finish(room)


def finish(room):
    b = room['bidder']
    bt = b % 2
    need = room['cur'][1]
    got = room['tw'][bt]
    m = room['mult']
    tag = {1: '', 2: ' (challenged x2)', 4: ' (rechallenged x4)'}[m]
    if got >= need:
        pts = need * 10 * m
        room['scores'][bt] += pts
        room['result'] = "Bid MADE (%d/%d). Team %s scores %d%s." % (got, need, 'AB'[bt], pts, tag)
    else:
        pts = (need - got) * 25 * m
        room['scores'][1 - bt] += pts
        room['result'] = "Bid FAILED (%d/%d). Team %s scores %d%s." % (got, need, 'AB'[1 - bt], pts, tag)
    log(room, room['result'])
    room['phase'] = 'handover'


def act(d):
    a = d.get('action')
    for k in [k for k, r in ROOMS.items() if time.time() - r.get('ts', 0) > 6 * 3600]:
        del ROOMS[k]                      # drop rooms idle for 6 hours
    if a == 'create':
        n = int(d['n'])
        if n not in (2, 4, 6, 8, 10):
            raise ValueError("Number of players must be even (2 to 10) so both teams are equal")
        room = new_room(n)
        tok = ''.join(random.choices(string.ascii_letters, k=12))
        room['players'].append(dict(name=d['name'][:14] or 'P1', token=tok))
        room['private'] = bool(d.get('private'))
        return dict(code=room['code'], token=tok)
    room = ROOMS.get(d.get('code', '').upper())
    if not room:
        raise ValueError("Room not found")
    if a == 'join':
        if room['phase'] != 'lobby' or len(room['players']) >= room['n']:
            raise ValueError("Room is full or already started")
        tok = ''.join(random.choices(string.ascii_letters, k=12))
        room['players'].append(dict(name=d['name'][:14] or 'P%d' % (len(room['players']) + 1), token=tok))
        if len(room['players']) == room['n']:      # last player in -> game starts automatically
            new_hand(room)
            log(room, "Cards removed to deal evenly: %s" % (', '.join(room['removed']) or 'none'))
        return dict(code=room['code'], token=tok)
    seat = next((i for i, p in enumerate(room['players']) if p['token'] == d.get('token')), None)
    if seat is None:
        raise ValueError("Not in this room")
    room['ts'] = time.time()
    if a == 'start':
        if seat != 0 or room['phase'] != 'lobby' or len(room['players']) < room['n']:
            raise ValueError("Host can start only when all seats are filled")
        new_hand(room)
        log(room, "Cards removed to deal evenly: %s" % (', '.join(room['removed']) or 'none'))
    elif a == 'bid':
        do_bid(room, seat, d)
    elif a == 'play':
        do_play(room, seat, d)
    elif a in ('challenge', 'skip'):
        if room['phase'] != 'challenge' or seat not in room['pending']:
            raise ValueError("Not allowed now")
        if a == 'challenge':
            room.update(mult=2, challenger=seat, pending=set())
            log(room, "%s CHALLENGES! Points x2." % room['players'][seat]['name'])
        else:
            room['pending'].discard(seat)
        if not room['pending']:
            room['phase'] = 'play'
            room['turn'] = room['bidder']
    elif a == 'rechallenge':
        if (room['phase'] != 'play' or room['mult'] != 2 or room['played'] != 0
                or seat % 2 != room['bidder'] % 2):
            raise ValueError("Rechallenge not allowed now")
        room.update(mult=4, rechal=True)
        log(room, "%s RECHALLENGES! Points x4." % room['players'][seat]['name'])
    elif a == 'close':
        if seat != 0:
            raise ValueError("Only the host can close the room")
        del ROOMS[room['code']]
    elif a == 'leave':
        if room['phase'] != 'lobby' or seat == 0:
            raise ValueError("You can only leave before the game starts")
        room['players'].pop(seat)
    elif a == 'next':
        if room['phase'] != 'handover':
            raise ValueError("Hand not finished")
        new_hand(room)
    elif a == 'end':
        if seat != 0:
            raise ValueError("Only the host can end the game")
        room['phase'] = 'over'
        s = room['scores']
        log(room, "GAME OVER. " + ("Team A wins!" if s[0] > s[1] else "Team B wins!" if s[1] > s[0] else "Draw!"))
    return {}


def view(room, tok):
    seat = next((i for i, p in enumerate(room['players']) if p['token'] == tok), None)
    v = {k: room[k] for k in ('code', 'n', 'phase', 'scores', 'mult', 'log', 'removed', 'k', 'turn',
                              'trick', 'last', 'bidder', 'trump', 'tw', 'played', 'result', 'challenger', 'rechal', 'bids', 'dealer')}
    v['players'] = [p['name'] for p in room['players']]
    v['seat'] = seat
    v['hand'] = room['hands'][seat] if seat is not None and room['hands'] else []
    v['counts'] = [len(h) for h in room['hands']]
    v['bid'] = room['cur']
    v['passed'] = sorted(room['passed'])
    v['pending'] = sorted(room['pending'])
    v['sup'] = (support_suit(room, seat) if seat is not None and room['phase'] == 'bidding'
                and room['turn'] == seat and seat in room['passed'] else None)
    return v


HTML = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NOTRUM</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;800&family=DM+Sans:wght@400;600;700&display=swap" rel=stylesheet>
<style>
:root{--ivory:#fbf7ec;--brass:#d8a647;--teal:#52b7c2;--heart:#b4232c;--ink:#1b1a17;--serif:'Playfair Display',Georgia,serif;--sans:'DM Sans',system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;padding:10px 10px 40px;color:var(--ivory);font-family:var(--sans);background:radial-gradient(ellipse at 50% 0,#2a1d12,#130d08 70%) fixed;min-height:100vh}
#app{max-width:1000px;margin:auto}button{font-family:inherit;cursor:pointer}button:disabled{opacity:.35;cursor:default}
h1.logo{font:800 56px var(--serif);margin:18px 0 0;text-align:center;letter-spacing:3px;color:var(--brass)}
.tag{text-align:center;opacity:.75;margin:4px 0 18px}
.panel{background:#00000055;border:1px solid #d8a64755;border-radius:14px;padding:16px;margin:12px auto;max-width:560px}
.panel h3{margin:0 0 10px;font:600 20px var(--serif)}
.panel input,.panel select{font:16px var(--sans);padding:9px 12px;border-radius:8px;border:1px solid #d8a64766;background:#1b130c;color:var(--ivory);margin:3px 4px 3px 0}
.panel button,.btn{background:var(--brass);color:#2a1a08;border:0;border-radius:8px;padding:10px 16px;font-weight:700;font-size:15px;margin:3px 0}
.room{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #fff2}
/* top bar */
.top{display:flex;align-items:stretch;gap:8px;margin-bottom:8px}
.sc{flex:1;border-radius:10px;padding:6px 14px;background:#0008;border-left:5px solid var(--tc);display:flex;justify-content:space-between;align-items:center;font-weight:600}
.sc b{font:800 28px var(--serif)}.sc.tB{flex-direction:row-reverse;border-left:0;border-right:5px solid var(--tc)}
.mid{text-align:center;padding:4px 10px;background:#0008;border-radius:10px;font-size:14px;min-width:150px}.mid small{display:block;opacity:.7;font-size:11px}
.tA{--tc:var(--brass)}.tB{--tc:var(--teal)}
/* contract banner */
.banner{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:10px 20px;margin-bottom:8px;border-radius:14px;background:linear-gradient(100deg,#00000099,#2a1a0acc);border:2px solid var(--tc)}
.bsm{font-size:13px;opacity:.85}.bbig{font:800 52px/1 var(--serif)}.bm{flex:1;min-width:170px;font-size:15px;line-height:1.5}
.mult{background:var(--heart);border-radius:10px;padding:6px 14px;font:800 30px var(--serif);text-align:center}.mult small{display:block;font:600 11px var(--sans)}
.rd{color:#ff6b6b}.banner .rd,.big .rd{color:#ff7b7b}
/* table */
.stage{position:relative;width:100%;aspect-ratio:16/10;margin:6px auto}
.table{position:absolute;inset:9% 6%;border-radius:50%;border:15px solid #4b2a16;outline:2px solid #d8a64766;outline-offset:-24px;
 background:radial-gradient(ellipse at 50% 38%,#18684c,#0e4a36 62%,#0a3828);box-shadow:0 0 0 3px #24130a,inset 0 0 70px #000a,0 24px 40px #000b}
.center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center;width:36%;pointer-events:none}
.cl{font-size:13px;opacity:.8}.big{font:800 68px/1.05 var(--serif);text-shadow:0 3px 8px #0009}.cs2{font-size:15px;margin-top:2px}
.code{font:800 54px var(--serif);letter-spacing:8px;color:var(--brass)}.res{font:600 20px var(--serif);line-height:1.3}
.seat{position:absolute;transform:translate(-50%,-50%);width:96px;text-align:center;z-index:3}
.av{width:46px;height:46px;border-radius:50%;margin:0 auto;display:grid;place-items:center;font:800 20px var(--serif);background:#2b2118;border:3px solid var(--tc)}
.nm{margin-top:3px;background:#000b;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cnt{font-size:11px;opacity:.8}.seat.empty{opacity:.45}.seat.empty .av{border-style:dashed}
.bub{display:inline-block;margin-top:2px;background:var(--ivory);color:var(--ink);border-radius:12px;padding:1px 10px;font:700 15px var(--serif)}.bub .rd{color:var(--heart)}
.dchip{position:absolute;top:-4px;right:12px;width:20px;height:20px;border-radius:50%;background:var(--brass);color:#2a1a08;font:800 11px/20px var(--sans)}
.seat.turn .av{box-shadow:0 0 0 4px #fff4,0 0 22px 8px var(--tc);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{box-shadow:0 0 0 4px #fff6,0 0 30px 12px var(--tc)}}
.tc{position:absolute;transform:translate(-50%,-50%) rotate(var(--r));--w:56px;z-index:2}.tc.old{opacity:.55}.tc.win{opacity:1;filter:drop-shadow(0 0 8px var(--brass))}
/* cards */
.pc{position:relative;width:var(--w);height:calc(var(--w)*1.45);font-size:calc(var(--w)*.2);background:linear-gradient(145deg,#fffdf6,#efe8d6);color:var(--ink);
 border-radius:calc(var(--w)*.1);box-shadow:0 2px 5px #0008,inset 0 0 0 1px #0002;font-family:var(--serif);user-select:none}
.pc.red{color:var(--heart)}
.ix{position:absolute;line-height:1;font-weight:800;text-align:center;font-size:1em}.tl{top:5%;left:6%}.br{bottom:5%;right:6%;transform:rotate(180deg)}
.pip{position:absolute;transform:translate(-50%,-50%);font-style:normal;font-size:1.25em;line-height:1}.pip.flip{transform:translate(-50%,-50%) rotate(180deg)}.pip.ace{font-size:3em}
.face{position:absolute;inset:11% 20%;border:1.5px solid currentColor;border-radius:4px;display:flex;flex-direction:column;align-items:center;justify-content:center;
 background:repeating-linear-gradient(45deg,#0000 0 4px,#0000000b 4px 8px)}.face b{font-size:2.6em;line-height:1}.face i{font-style:normal;font-size:1.5em}
/* hand */
.hand{display:flex;justify-content:center;padding:26px 0 8px;min-height:150px}
.hc{transform:rotate(var(--rot));transform-origin:50% 120%;transition:transform .15s;flex:none}
.hc .pc{box-shadow:0 3px 8px #000a,inset 0 0 0 1px #0002}.hc.no .pc{filter:brightness(.62)}
.hc.ok{cursor:pointer}.hc.ok .pc{box-shadow:0 0 0 2px var(--brass),0 3px 10px #000a}.hc.ok:hover{transform:translateY(-22px) rotate(var(--rot));z-index:5}
/* dock */
.menu{text-align:right;margin-bottom:6px}.menu button{padding:6px 14px;font-size:13px}
.dock{text-align:center;margin:8px 0;min-height:56px}.msg{padding:10px;opacity:.9}.hint{font-size:13px;opacity:.75;margin-top:6px}
.bidpad{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap}
.stp{display:flex;align-items:center;gap:8px;background:#0007;border-radius:12px;padding:4px 8px}.stp span{font:800 30px var(--serif);min-width:40px}
.stp button{width:36px;height:36px;border-radius:50%;border:0;background:var(--brass);font-size:22px;font-weight:700;color:#2a1a08}
.suits{display:flex;gap:6px}.sb{min-width:52px;height:52px;border-radius:10px;border:2px solid transparent;background:var(--ivory);color:var(--ink);font:800 20px var(--serif)}
.sb.rd{color:var(--heart)}.sb.sel{border-color:var(--brass);box-shadow:0 0 0 3px #d8a64766;transform:translateY(-3px)}
.go{background:var(--brass);color:#2a1a08;border:0;border-radius:10px;padding:13px 22px;font-weight:700;font-size:16px}
.pass{background:transparent;color:var(--ivory);border:1.5px solid #fff6;border-radius:10px;padding:12px 20px;font-weight:600;font-size:16px}
.alt{background:var(--heart);color:#fff;border:0;border-radius:10px;padding:12px 20px;font-weight:700;font-size:16px;margin:3px}
details{max-width:560px;margin:10px auto;background:#0006;border-radius:10px;padding:8px 12px;font-size:13px}summary{cursor:pointer;opacity:.8}
@media(max-width:700px){h1.logo{font-size:40px}.stage{aspect-ratio:4/5.3}.table{inset:8% 4%;border-width:10px;outline-offset:-16px}.seat{width:70px}.av{width:34px;height:34px;font-size:15px}
.nm{font-size:10px;padding:1px 5px}.big{font-size:42px}.code{font-size:34px;letter-spacing:4px}.tc{--w:40px}.bbig{font-size:38px}.top{flex-wrap:wrap}.mid{order:-1;width:100%}.res{font-size:14px}.center{width:46%}.cl,.cs2{font-size:11px}}
@media(prefers-reduced-motion:reduce){.seat.turn .av{animation:none}}
</style></head><body><div id=app></div><script>
const SY={S:'♠',H:'♥',D:'♦',C:'♣'},SN={S:'Spades',H:'Hearts',D:'Diamonds',C:'Clubs',N:'No Trump'},RN={11:'J',12:'Q',13:'K',14:'A'},SV={N:5,S:4,H:3,D:2,C:1};
const PIPS={2:[[1,0],[1,6]],3:[[1,0],[1,3],[1,6]],4:[[0,0],[2,0],[0,6],[2,6]],5:[[0,0],[2,0],[1,3],[0,6],[2,6]],6:[[0,0],[2,0],[0,3],[2,3],[0,6],[2,6]],
7:[[0,0],[2,0],[1,1.5],[0,3],[2,3],[0,6],[2,6]],8:[[0,0],[2,0],[1,1.5],[0,3],[2,3],[1,4.5],[0,6],[2,6]],
9:[[0,0],[2,0],[0,2],[2,2],[1,3],[0,4],[2,4],[0,6],[2,6]],10:[[0,0],[2,0],[1,1],[0,2],[2,2],[0,4],[2,4],[1,5],[0,6],[2,6]]};
const UC=(new URLSearchParams(location.search).get('code')||'').toUpperCase();
let selfClose=0,code=sessionStorage.nc||'',tok=sessionStorage.nt||'',S=null,prev='',bn=1,bs='S',logOpen=false;
const $=id=>document.getElementById(id),sym=s=>s=='N'?'NT':SY[s],isR=s=>'HD'.includes(s);
const bidH=(n,s)=>`${n}<span class="${isR(s)?'rd':''}"> ${sym(s)}</span>`;
async function api(o){const r=await fetch('/api',{method:'POST',body:JSON.stringify({...o,code,token:tok})});
 const j=await r.json();if(j.error){alert(j.error);if(!tok)code='';return null}
 if(j.token){code=j.code;tok=j.token;sessionStorage.nc=code;sessionStorage.nt=tok}poll();return j}
function cardH(c){const s=SY[c.s],r=RN[c.r]||c.r;let m='';
 if(c.r==14)m=`<i class="pip ace" style="left:50%;top:50%">${s}</i>`;
 else if(c.r>10)m=`<div class=face><b>${r}</b><i>${s}</i></div>`;
 else m=PIPS[c.r].map(([x,y])=>`<i class="pip${y>3?' flip':''}" style="left:${[25,50,75][x]}%;top:${14+y*12}%">${s}</i>`).join('');
 return `<div class="pc ${isR(c.s)?'red':''}"><span class="ix tl">${r}<br>${s}</span><span class="ix br">${r}<br>${s}</span>${m}</div>`}
function home(){return `<h1 class=logo>NOTRUM</h1><div class=tag>Team trick-taking with bids, challenges and rechallenges</div>
<div class=panel><h3>Start a new game</h3>Your name <input id=nm maxlength=14><br>Players <select id=np><option>2<option>4<option selected>6<option>8<option>10</select>
<label><input type=checkbox id=pv> Private</label><br><button onclick="api({action:'create',name:$('nm').value,n:$('np').value,private:$('pv').checked})">Create game and get room code</button></div>
<div class=panel><h3>Join a game</h3>Your name <input id=nm2 maxlength=14><br>Room code <input id=rc size=5 value="${(new URLSearchParams(location.search).get('code')||'').toUpperCase()}" style="text-transform:uppercase">
<button onclick="joinRoom($('rc').value.toUpperCase())">Join</button></div>
<div class=panel><h3>Open games</h3><div id=rooms>Loading…</div></div>`}
async function loadRooms(){try{const r=await(await fetch('/rooms')).json();const el=$('rooms');if(!el)return;
 el.innerHTML=r.length?r.map(x=>`<div class=room><span>${x.host}'s game · ${x.have}/${x.n} players · <b>${x.code}</b></span><button class=btn onclick="joinRoom('${x.code}')">Join</button></div>`).join(''):'No open games right now. Start one above.'}catch(e){}}
function joinRoom(c){const n=$('nm2').value.trim();if(!n)return alert('Enter your name first');code=c;tok='';api({action:'join',name:n,code:c})}
function goHome(){code='';tok='';sessionStorage.clear();S=null;prev='';poll()}
function menuH(){const host=S.seat==0;
 if(S.phase=='lobby')return host?`<button class=pass onclick="if(confirm('Close this room? Everyone will be sent back to the home page.')){selfClose=1;api({action:'close'})}">Close room</button>`:`<button class=pass onclick="api({action:'leave'}).then(j=>j&&goHome())">Leave room</button>`;
 if(S.phase=='over')return (host?`<button class=pass onclick="selfClose=1;api({action:'close'})">Close room</button> `:'')+`<button class=go onclick="goHome()">Back to home</button>`;
 return host?`<button class=pass onclick="if(confirm('End the game now and show the final result?'))api({action:'end'})">End game</button>`:''}
function setN(d){bn=Math.max(1,Math.min(S.k,bn+d));render()}function setS(s){bs=s;render()}
function pos(i,rx,ry){const n=S.n,me=S.seat??0,k=(i-me+n)%n,t=(90+k*360/n)*Math.PI/180;return[50+rx*Math.cos(t),50+ry*Math.sin(t)]}
function seatsH(){let h='';const live=['bidding','challenge','play'].includes(S.phase);
 for(let i=0;i<S.n;i++){const[x,y]=pos(i,47,45),p=S.players[i],tm=i%2?'B':'A',b=S.bids[i];
  h+=`<div class="seat t${tm} ${p?'':'empty'} ${live&&S.turn==i?'turn':''}" style="left:${x}%;top:${y}%"><div class=av>${p?p[0].toUpperCase():'?'}</div>
  <div class=nm>${p?p+(i==S.seat?' (you)':''):'Empty seat'}</div><div class=cnt>${S.phase=='lobby'?'Team '+tm:S.counts[i]+' cards'}</div>
  ${b&&['bidding','challenge'].includes(S.phase)?`<div class=bub>${b=='P'?'Pass':bidH(b[0],b[1])}</div>`:''}
  ${S.phase!='lobby'&&S.dealer==i?'<span class=dchip>D</span>':''}</div>`}return h}
function trickH(){const cs=S.trick,src=cs.length?cs:(S.last?S.last.cards:[]);
 return src.map(c=>{const[x,y]=pos(c.seat,21,19);return `<div class="tc ${cs.length?'':'old'} ${!cs.length&&S.last.winner==c.seat?'win':''}" style="left:${x}%;top:${y}%;--r:${(c.seat*37%17)-8}deg">${cardH(c)}</div>`}).join('')}
function centerH(){const P=S.players,b=S.bid;
 if(S.phase=='lobby')return `<div class=cl>Room code</div><div class=code>${S.code}</div><div class=cs2>${P.length} of ${S.n} seated</div><div class=cl>The game starts when every seat is filled</div>`;
 if(S.phase=='bidding')return `<div class=cl>${b?'Current bid':'Bidding open'}</div>`+(b?`<div class=big>${bidH(b[1],b[2])}</div><div class=cs2>${P[S.bidder]}</div>`:`<div class=big>–</div><div class=cs2>${P[S.turn]} opens the bidding</div>`);
 if(S.phase=='challenge')return `<div class=cl>Bid won by ${P[S.bidder]}</div><div class=big>${bidH(b[1],b[2])}</div><div class=cs2>Waiting for challenge</div>`;
 if(S.phase=='handover')return `<div class=res>${S.result}</div>`;
 if(S.phase=='over'){const w=S.scores[0]>S.scores[1]?'Team A wins':S.scores[1]>S.scores[0]?'Team B wins':'Draw';return `<div class=big style="font-size:44px">${w}</div><div class=cs2>${S.scores[0]} – ${S.scores[1]}</div>`}
 if(!S.trick.length&&S.last)return `<div class=cs2>${P[S.last.winner]} takes the trick</div>`;return ''}
function bannerH(){if(!S.bid||!['challenge','play','handover'].includes(S.phase))return '';
 const bt=S.bidder%2,tm='AB'[bt],o='AB'[1-bt],n=S.bid[1],s=S.bid[2],m=S.mult;
 return `<div class="banner t${tm}"><div><div class=bsm>Winning bid: ${S.players[S.bidder]} · Team ${tm}</div><div class=bbig>${bidH(n,s)}</div></div>
 <div class=bm><b>${s=='N'?'No trump':SN[s]+' are trump'}</b><br>Team ${tm} needs ${n} hand${n>1?'s':''} and has ${S.tw[bt]}<br>Team ${o} has ${S.tw[1-bt]}</div>
 ${m>1?`<div class=mult>×${m}<small>${m==2?'Challenged by '+S.players[S.challenger]:'Rechallenged'}</small></div>`:''}</div>`}
function dockH(){const me=S.seat,P=S.players,ph=S.phase;
 if(ph=='bidding'){if(S.turn!=me)return `<div class=msg>Waiting for ${P[S.turn]} to bid…</div>`;
  const cur=S.bid?S.bid[0]:0,ok=bn*10+SV[bs]>cur;
  return (S.sup?`<div class=msg>You passed earlier, but you can still support your teammate's call in ${sym(S.sup)}. Raise the hands, or stay out.</div>`:'')+`<div class=bidpad><div class=stp><button onclick="setN(-1)">−</button><span>${bn}</span><button onclick="setN(1)">+</button></div>
  <div class=suits>${['N','S','H','D','C'].map(x=>`<button class="sb ${isR(x)?'rd':''} ${bs==x?'sel':''}" ${bn*10+SV[x]<=cur||(S.sup&&x!=S.sup)?'disabled':''} onclick="setS('${x}')">${sym(x)}</button>`).join('')}</div>
  <button class=go ${ok?'':'disabled'} onclick="api({action:'bid',n:bn,suit:bs})">Bid ${bn} ${sym(bs)}</button><button class=pass onclick="api({action:'bid',pass:1})">${S.sup?'Stay out':'Pass'}</button></div>
  ${ok?`<div class=hint>Choose hands and a suit. NT outranks ♠ ♥ ♦ ♣.</div>`:'<div class=hint>Raise the number, or pick a higher suit.</div>'}`}
 if(ph=='challenge')return S.pending.includes(me)?`<div class=msg>Think ${P[S.bidder]}'s team will fall short?</div><button class=alt onclick="api({action:'challenge'})">Challenge ×2</button><button class=pass onclick="api({action:'skip'})">No challenge</button>`:`<div class=msg>The opposing team is deciding whether to challenge…</div>`;
 if(ph=='play'){let h='';if(S.mult==2&&S.played==0&&me%2==S.bidder%2)h+=`<div class=msg>${P[S.challenger]} challenged your team.</div><button class=alt onclick="api({action:'rechallenge'})">Rechallenge ×4</button>`;
  return h+`<div class=msg>${S.turn==me?'<b>Your turn.</b> Play a card.':'Waiting for '+P[S.turn]+'…'}</div>`}
 if(ph=='handover')return `<button class=go onclick="api({action:'next'})">Deal next hand</button>${me==0?` <button class=pass onclick="if(confirm('End the game?'))api({action:'end'})">End game</button>`:''}`;return ''}
function handH(){const n=S.hand.length;if(!n)return '';
 const w=innerWidth<700?58:84,av=Math.min(innerWidth-24,980),st=Math.min(w*.7,(av-w)/Math.max(1,n-1));
 const led=S.trick.length?S.trick[0].s:null,has=led&&S.hand.some(c=>c.s==led),mine=S.phase=='play'&&S.turn==S.seat;
 return `<div class=hand style="--w:${w}px">`+S.hand.map((c,i)=>{const ok=mine&&(!has||c.s==led);
  return `<div class="hc ${ok?'ok':mine?'no':''}" style="margin-left:${i?st-w:0}px;--rot:${(i-(n-1)/2)*(n>16?1:2.2)}deg" onclick="${ok?`api({action:'play',r:${c.r},s:'${c.s}'})`:''}">${cardH(c)}</div>`}).join('')+`</div>`}
function render(){if(!S){$('app').innerHTML=home();loadRooms();return}
 if(S.sup)bs=S.sup;
 if(S.phase=='bidding'&&S.turn==S.seat&&S.bid&&bn*10+SV[bs]<=S.bid[0])bn=Math.min(S.k,S.bid[1]+(SV[bs]>SV[S.bid[2]]?0:1));
 const rm=S.phase!='lobby'?`<small>Removed: ${S.removed.join(', ')||'none'} · ${S.k} cards each</small>`:'';
 $('app').innerHTML=`<div class=top><div class="sc tA">Team A<b>${S.scores[0]}</b></div><div class=mid>Room <b>${S.code}</b>${rm}</div><div class="sc tB">Team B<b>${S.scores[1]}</b></div></div>
 <div class=menu>${menuH()}</div>${bannerH()}<div class=stage><div class=table></div><div class=center>${centerH()}</div>${trickH()}${seatsH()}</div>
 <div class=dock>${dockH()}</div>${handH()}
 <details ${logOpen?'open':''} ontoggle="logOpen=this.open"><summary>Game log</summary>${S.log.slice().reverse().join('<br>')}</details>`}
async function poll(){if(!code){if(prev!='home'){prev='home';S=null;render()}return loadRooms()}
 if(!tok)return;
 const mc=code,mt=tok;
 try{const r=await fetch(`/state?code=${mc}&token=${mt}`);const j=await r.json();
 if(mc!==code||mt!==tok)return;
 if(j.error){if(S&&!selfClose)alert('This room was closed by the host.');selfClose=0;return goHome()}
 if(j.seat===null||j.seat===undefined){selfClose=1;return goHome()}
 const s=JSON.stringify(j);if(s!=prev){prev=s;S=j;render()}}catch(e){}}
if(UC&&UC!=code){code='';tok='';sessionStorage.clear()}
onresize=()=>S&&render();render();poll();setInterval(poll,1000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype='application/json', code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/rooms':
            with LOCK:
                out = [dict(code=r['code'], host=r['players'][0]['name'], have=len(r['players']), n=r['n'])
                       for r in ROOMS.values()
                       if r['phase'] == 'lobby' and not r.get('private') and r['players']]
            return self._send(json.dumps(out))
        if u.path == '/state':
            q = parse_qs(u.query)
            with LOCK:
                room = ROOMS.get(q.get('code', [''])[0].upper())
                if not room:
                    return self._send(json.dumps({'error': 'no room'}))
                room['ts'] = time.time()
                return self._send(json.dumps(view(room, q.get('token', [''])[0])))
        self._send(HTML, 'text/html')

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        with LOCK:
            try:
                self._send(json.dumps(act(d)))
            except (ValueError, KeyError) as e:
                self._send(json.dumps({'error': str(e)}))

    def log_message(self, *a):
        pass


def lan_ip():
    try:
        x = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        x.connect(('10.255.255.255', 1))
        ip = x.getsockname()[0]
        x.close()
        return ip
    except Exception:
        return '127.0.0.1'


def main():
    import os
    port = int(os.environ.get('PORT', 8000))      # hosting services set PORT automatically
    print("=" * 60)
    print(" NOTRUM website is running on port %d" % port)
    print(" On this PC:           http://localhost:%d" % port)
    print(" Same Wi-Fi/LAN:       http://%s:%d" % (lan_ip(), port))
    print(" Anyone, anywhere:     use your public hosting/tunnel URL")
    print("=" * 60)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()


if __name__ == '__main__':
    main()
