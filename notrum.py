#!/usr/bin/env python3
"""
NOTRUM - team trick-taking card game with bidding, challenge & rechallenge.
Run:  python3 notrum.py      then everyone opens  http://<host-ip>:8000
No external libraries needed (Python 3.8+).
"""
import json, random, string, threading, socket
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
                result='', k=0, trump=None)
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
                rechal=False, trick=[], last=None, result='', trump=None)
    log(room, "New hand dealt. %d cards each. Bidding starts with %s." %
        (room['k'], room['players'][room['turn']]['name']))


def nxt_active(room, seat):
    s = seat
    while True:
        s = (s + 1) % room['n']
        if s not in room['passed']:
            return s


def do_bid(room, seat, d):
    if room['phase'] != 'bidding' or room['turn'] != seat:
        raise ValueError("Not your turn to bid")
    name = room['players'][seat]['name']
    if d.get('pass'):
        room['passed'].add(seat)
        log(room, "%s passes." % name)
    else:
        n, s = int(d['n']), d['suit']
        if s not in STR or not 1 <= n <= room['k']:
            raise ValueError("Invalid bid")
        v = n * 10 + STR[s]
        if room['cur'] and v <= room['cur'][0]:
            raise ValueError("Bid must be higher: raise the number, or keep the number with a higher suit")
        room['cur'] = (v, n, s)
        room['bidder'] = seat
        log(room, "%s bids %d %s." % (name, n, 'No Trump' if s == 'N' else s))
    active = [i for i in range(room['n']) if i not in room['passed']]
    if not active:
        log(room, "Everyone passed - redealing.")
        new_hand(room)
    elif room['cur'] and len(active) == 1:
        b = room['bidder']
        room['trump'] = room['cur'][2]
        room['pending'] = {i for i in range(room['n']) if i % 2 != b % 2}
        room['phase'] = 'challenge'
        room['turn'] = b
        log(room, "%s wins the bid: %d %s. Opponents may challenge." %
            (room['players'][b]['name'], room['cur'][1], room['trump']))
    else:
        room['turn'] = nxt_active(room, seat)


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
                              'trick', 'last', 'bidder', 'trump', 'tw', 'played', 'result', 'challenger', 'rechal')}
    v['players'] = [p['name'] for p in room['players']]
    v['seat'] = seat
    v['hand'] = room['hands'][seat] if seat is not None and room['hands'] else []
    v['counts'] = [len(h) for h in room['hands']]
    v['bid'] = room['cur']
    v['passed'] = sorted(room['passed'])
    v['pending'] = sorted(room['pending'])
    return v


HTML = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NOTRUM</title><style>
body{font-family:system-ui,sans-serif;background:#0b5d3b;color:#fff;margin:0;padding:12px;max-width:900px;margin:auto}
button,input,select{font-size:16px;padding:7px 12px;border-radius:6px;border:0;margin:3px}
button{background:#f5c542;cursor:pointer}button:disabled{opacity:.4}
.card{display:inline-block;background:#fff;color:#111;border-radius:6px;padding:8px 6px;margin:3px;min-width:38px;text-align:center;font-weight:bold;border:2px solid #fff}
.red{color:#c00}.pbtn{cursor:pointer}.dis{opacity:.35}.box{background:#0003;padding:10px;border-radius:8px;margin:8px 0}
.turn{outline:3px solid #f5c542}small{opacity:.8}#log{font-size:13px;max-height:150px;overflow:auto}
</style></head><body><h2>♠♥ NOTRUM ♦♣</h2><div id=app></div><script>
const SY={S:'♠',H:'♥',D:'♦',C:'♣',N:'No Trump'},RN={11:'J',12:'Q',13:'K',14:'A'};
let code=localStorage.nc||'',tok=localStorage.nt||'',S=null,prev='';
const $=id=>document.getElementById(id);
async function api(o){const r=await fetch('/api',{method:'POST',body:JSON.stringify({...o,code,token:tok})});
 const j=await r.json();if(j.error){alert(j.error);if(!tok)code='';return null}
 if(j.token){code=j.code;tok=j.token;localStorage.nc=code;localStorage.nt=tok}poll();return j}
const cardH=(c,x='')=>`<span class="card ${'HD'.includes(c.s)?'red':''} ${x}">${RN[c.r]||c.r}${SY[c.s]}</span>`;
function home(){return `<div class=box><h3>Start a new game (you become the admin)</h3>Your name <input id=nm><br>Number of players (even)
 <select id=np><option>2<option>4<option selected>6<option>8<option>10</select>
 <br><label><input type=checkbox id=pv> Private game (hide from the open games list)</label><br>
 <button onclick="api({action:'create',name:$('nm').value,n:$('np').value,private:$('pv').checked})">Create game &amp; get room code</button></div>
 <div class=box><h3>Join room</h3>Name <input id=nm2> Room code <input id=rc size=5 value="${(new URLSearchParams(location.search).get('code')||'').toUpperCase()}" style="text-transform:uppercase">
 <button onclick="joinRoom($('rc').value.toUpperCase())">Join</button></div>
 <div class=box><h3>Open games waiting for players</h3><div id=rooms><small>Loading...</small></div></div>`}
function render(){
 if(!S){$('app').innerHTML=home();return}
 const me=S.seat,t=S.turn,P=S.players,tm=i=>'AB'[i%2];let h='';
 h+=`<div class=box>Room <b>${S.code}</b> · You: <b>${P[me]}</b> (Team ${tm(me)}) · Score — Team A: <b>${S.scores[0]}</b> | Team B: <b>${S.scores[1]}</b>
 <small>(seat order = table order; teams alternate)</small></div>`;
 if(S.phase=='lobby'){
  h+=`<div class=box>${me==0?`<div style="font-size:15px">You are the admin. Give this room code to the other players:</div><div style="font-size:44px;letter-spacing:8px;font-weight:bold">${S.code}</div>`:''}Waiting for players: ${P.length}/${S.n}<br>${P.map((p,i)=>`${i+1}. ${p} (Team ${tm(i)})`).join('<br>')}<br>
  <br><small>Share room code <b>${S.code}</b>. The game starts automatically when all ${S.n} players have joined.</small></div>`}
 else{
  h+=`<div class=box>Cards removed: <b>${S.removed.join(', ')||'none'}</b> · ${S.k} cards each · `+
   (S.bid?`Contract: <b>${S.bid[1]} ${S.trump=='N'?'No Trump':SY[S.trump]||''}</b> by ${P[S.bidder]} (Team ${tm(S.bidder)}) · multiplier x${S.mult}<br>Tricks — A: ${S.tw[0]} | B: ${S.tw[1]}`:'No bid yet')+`</div>`;
  h+=`<div class=box>`+P.map((p,i)=>`<span class="card ${i==t&&S.phase!='handover'&&S.phase!='over'?'turn':''}" style="font-weight:normal">${p}<br><small>T${tm(i)} · ${S.counts[i]} cards${S.passed.includes(i)?' · passed':''}</small></span>`).join('')+`</div>`;
  if(S.phase=='bidding'){
   h+=`<div class=box><b>Bidding</b> ${t==me?'— your turn':'— waiting for '+P[t]}<br>`;
   if(t==me)h+=`Hands <input id=bn type=number min=1 max=${S.k} value=${S.bid?S.bid[1]:1} style="width:60px">
   <select id=bs><option value=N>No Trump<option value=S>♠ Spades<option value=H>♥ Hearts<option value=D>♦ Diamonds<option value=C>♣ Clubs</select>
   <button onclick="api({action:'bid',n:$('bn').value,suit:$('bs').value})">Bid</button>
   <button onclick="api({action:'bid',pass:1})">Pass</button>`;h+=`</div>`}
  if(S.phase=='challenge'){
   h+=`<div class=box><b>Challenge window</b> — opponents of ${P[S.bidder]} may challenge (x2) before play.<br>`;
   if(S.pending.includes(me))h+=`<button onclick="api({action:'challenge'})">Challenge x2</button><button onclick="api({action:'skip'})">No challenge</button>`;
   else h+=`<small>Waiting...</small>`;h+=`</div>`}
  if(S.phase=='play'&&S.mult==2&&S.played==0&&me%2==S.bidder%2)
   h+=`<div class=box>${P[S.challenger]} challenged! <button onclick="api({action:'rechallenge'})">Rechallenge x4</button></div>`;
  if(['play','handover','over'].includes(S.phase)){
   h+=`<div class=box><b>Table</b><br>${S.trick.map(c=>`<span>${P[c.seat]}:</span>${cardH(c)}`).join(' ')||'<small>(empty)</small>'}`+
    (S.last?`<br><small>Last trick won by ${P[S.last.winner]}: </small>${S.last.cards.map(c=>cardH(c)).join('')}`:'')+`</div>`}
  if(S.phase=='handover')h+=`<div class=box><b>${S.result}</b><br><button onclick="api({action:'next'})">Next hand</button>
   ${me==0?'<button onclick="if(confirm(\'End game?\'))api({action:\'end\'})">End game</button>':''}</div>`;
  if(S.phase=='over')h+=`<div class=box><h3>Game over — ${S.scores[0]>S.scores[1]?'Team A wins!':S.scores[1]>S.scores[0]?'Team B wins!':'Draw!'}</h3></div>`;
  const led=S.trick.length?S.trick[0].s:null,has=led&&S.hand.some(c=>c.s==led);
  h+=`<div class=box><b>Your hand</b><br>`+S.hand.map(c=>{const ok=S.phase=='play'&&t==me&&(!has||c.s==led);
   return `<span onclick="${ok?`api({action:'play',r:${c.r},s:'${c.s}'})`:''}">${cardH(c,ok?'pbtn':'dis')}</span>`}).join('')+`</div>`}
 h+=`<div class=box id=log>${S.log.slice().reverse().join('<br>')}</div>`;
 $('app').innerHTML=h}
async function loadRooms(){try{const r=await(await fetch('/rooms')).json();const el=$('rooms');if(!el)return;
 el.innerHTML=r.length?r.map(x=>`<div>${x.host}'s game · ${x.have}/${x.n} players · code <b>${x.code}</b>
 <button onclick="joinRoom('${x.code}')">Join</button></div>`).join(''):'<small>No open games right now. Start one above!</small>'}catch(e){}}
function joinRoom(c){const n=$('nm2').value.trim();if(!n)return alert('Enter your name first');code=c;tok='';api({action:'join',name:n,code:c})}
async function poll(){if(!code){if(prev!='home'){prev='home';S=null;render()}return loadRooms()}
 try{const r=await fetch(`/state?code=${code}&token=${tok}`);const j=await r.json();
 if(j.error){code='';tok='';localStorage.clear();S=null;prev='';return poll()}
 const s=JSON.stringify(j);if(s!=prev){prev=s;S=j;render()}}catch(e){}}
render();poll();setInterval(poll,1000);
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
