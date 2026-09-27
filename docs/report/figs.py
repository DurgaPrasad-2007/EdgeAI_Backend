import json, os, statistics
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle, Polygon

D = json.load(open("data.json"))
os.makedirs("fig", exist_ok=True)
PERIOD = 0.6
INK, GREY, LIGHT, ACC = "#1b1b1b", "#6b6b6b", "#e9e6e1", "#b4451a"
RC = {"AMR-01": "#c2541a", "AMR-02": "#c48a00", "AMR-03": "#2a7fc0"}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.edgecolor": "#444", "axes.linewidth": 0.7, "axes.spines.top": False, "axes.spines.right": False,
                     "savefig.dpi": 300, "figure.dpi": 100, "axes.titlesize": 9, "axes.titleweight": "bold"})


def save(fig, name):
    fig.savefig(f"fig/{name}.png", bbox_inches="tight", facecolor="white", pad_inches=0.06)
    plt.close(fig)


def box(ax, x, y, w, h, text, fc="#ffffff", ec=INK, lw=0.9, ls="-", fs=7.5, bold=False, tc=INK, r=0.02, align="center", va="center"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw, ls=ls))
    ax.text(x + (w / 2 if align == "center" else 0.012), y + h / 2, text, ha=align, va=va, fontsize=fs, color=tc, fontweight="bold" if bold else "normal", linespacing=1.25)


def arrow(ax, p, q, text=None, color=INK, ls="-", lw=0.9, style="-|>", fs=6.5, off=(0, 0.012), rad=0.0):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=8, color=color, lw=lw, ls=ls, connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text((p[0] + q[0]) / 2 + off[0], (p[1] + q[1]) / 2 + off[1], text, fontsize=fs, ha="center", va="bottom", color=color)


# ---------------------------------------------------------------- Fig 1: warehouse graph
def fig_map():
    g = D["graph"]
    N = {n["id"]: n for n in g["nodes"]}
    fig, ax = plt.subplots(figsize=(6.9, 4.5))
    for a, b in g["edges"]:
        ax.plot([N[a]["x"], N[b]["x"]], [N[a]["y"], N[b]["y"]], color="#b8b3ab", lw=1.1, zorder=1)
    for e in (("BYPASS-WM", "DETOUR-SW"), ("DETOUR-SW", "DETOUR-S"), ("DETOUR-S", "DETOUR-SE"), ("DETOUR-SE", "BYPASS-EM")):
        ax.plot([N[e[0]]["x"], N[e[1]]["x"]], [N[e[0]]["y"], N[e[1]]["y"]], color="#3b7fbf", lw=1.6, ls=(0, (4, 2)), zorder=2)
    for n in g["nodes"]:
        k, x, y = n["kind"], n["x"], n["y"]
        if k == "rack":
            ax.add_patch(Rectangle((x - 22, y - 11), 44, 22, fc="#f1ede6", ec="#7d766c", lw=0.8, zorder=3))
        elif k == "dock":
            ax.add_patch(Rectangle((x - 26, y - 13), 52, 26, fc="#dfe9f3", ec="#2a5f94", lw=0.9, zorder=3))
        elif k == "charge":
            ax.add_patch(Rectangle((x - 26, y - 13), 52, 26, fc="#e4f1e0", ec="#2e7d32", lw=0.9, zorder=3))
        elif k == "corridor":
            ax.add_patch(Circle((x, y), 21, fc="#fbe3d6", ec=ACC, lw=1.6, zorder=3))
        else:
            ax.add_patch(Circle((x, y), 7, fc="#ffffff", ec="#555", lw=0.8, zorder=3))
        lab = n["id"].replace("RACK ", "").replace("INT-", "").replace("DETOUR-", "D-").replace("BYPASS-", "B-")
        if k in ("rack", "dock", "charge", "corridor"):
            ax.text(x, y, lab, ha="center", va="center", fontsize=5.6, zorder=4, color=INK, fontweight="bold" if k == "corridor" else "normal")
        else:
            ax.text(x, y - 13, lab, ha="center", va="bottom", fontsize=5.0, color="#444", zorder=4)
    b = N["AISLE-B07"]
    ax.add_patch(Circle((b["x"], b["y"]), 12, fc="none", ec="#c00000", lw=1.2, ls=(0, (2, 1.5)), zorder=5))
    ax.annotate("AISLE-B07\n(operator-blockable)", (b["x"], b["y"] + 12), xytext=(b["x"] + 40, b["y"] - 62), fontsize=6.3, color="#c00000", arrowprops=dict(arrowstyle="-", color="#c00000", lw=0.7))
    ax.annotate("C-14: only mutex zone\n(single-lane corridor)", (N["C-14"]["x"] + 20, N["C-14"]["y"] - 12), xytext=(N["C-14"]["x"] + 78, N["C-14"]["y"] - 105), fontsize=6.3, color=ACC, arrowprops=dict(arrowstyle="-", color=ACC, lw=0.7))
    for r in g["robots"]:
        h = N[r["home"]]
        ax.plot(h["x"], h["y"] + 21, marker="v", color=RC[r["id"]], ms=6, zorder=6)
        ax.text(h["x"], h["y"] + 32, r["id"], fontsize=5.8, ha="center", va="top", color=RC[r["id"]], fontweight="bold")
    ax.plot([], [], color="#3b7fbf", ls=(0, (4, 2)), lw=1.6, label="perimeter detour lane around B-07")
    ax.plot([], [], marker="v", color="#444", ls="", ms=5, label="robot home (docked start)")
    ax.legend(loc="lower left", fontsize=6.2, frameon=False, bbox_to_anchor=(0.0, -0.02))
    ax.set_xlim(40, 960); ax.set_ylim(575, 45); ax.set_aspect("equal"); ax.axis("off")
    save(fig, "f1_map")


# ---------------------------------------------------------------- Fig 2: architecture
def fig_arch():
    fig, ax = plt.subplots(figsize=(6.9, 4.9))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    box(ax, 0.03, 0.90, 0.30, 0.08, "Dashboard (Next.js)\nread-only observer", fc="#eef3f8", fs=7)
    box(ax, 0.03, 0.06, 0.30, 0.08, "Database\nSQLite / PostgreSQL + pgvector", fc="#f3f0ea", fs=7)
    ax.add_patch(FancyBboxPatch((0.03, 0.19), 0.94, 0.66, boxstyle="round,pad=0,rounding_size=0.015", fc="#fbfaf8", ec=INK, lw=1.2))
    ax.text(0.045, 0.83, "ONE Python process (FastAPI / uvicorn)", fontsize=7.5, fontweight="bold", va="top")
    box(ax, 0.06, 0.66, 0.24, 0.12, "Gateway\nREST + WebSocket, JWT,\nwrite-behind persistence", fc="#f1ede6", fs=6.6)
    box(ax, 0.38, 0.66, 0.24, 0.12, "Task board (\"WMS\")\nannounces queued tasks;\nhears awards & status", fc="#f1ede6", fs=6.6)
    box(ax, 0.70, 0.66, 0.24, 0.12, "World (ground truth)\npositions, blocked aisles,\nproximity monitor", fc="#f1ede6", fs=6.6)
    box(ax, 0.06, 0.45, 0.88, 0.075, "PeerBus: in-process publish/subscribe (broadcast + direct, per-robot inbox, radio-silence switch)", fc="#fbe3d6", ec=ACC, lw=1.3, fs=7, bold=True)
    for i, rid in enumerate(("AMR-01", "AMR-02", "AMR-03")):
        x = 0.06 + i * 0.30
        box(ax, x, 0.235, 0.27, 0.16, f"RobotAgent {rid}\nown asyncio control loop (0.6 s)\nprivate: pose, path, claims,\npeer table, auctions, task", fc="#ffffff", ec=RC[rid], lw=1.5, fs=6.3)
        arrow(ax, (x + 0.135, 0.395), (x + 0.135, 0.45), style="<|-|>", lw=0.9)
    arrow(ax, (0.18, 0.66), (0.18, 0.525), style="<|-|>")
    arrow(ax, (0.50, 0.66), (0.50, 0.525), style="<|-|>")
    arrow(ax, (0.82, 0.66), (0.82, 0.525), style="<|-|>", ls=(0, (3, 2)), color=GREY)
    arrow(ax, (0.18, 0.90), (0.18, 0.78), style="<|-|>", text="REST / WS", off=(0.06, -0.006))
    arrow(ax, (0.18, 0.19), (0.18, 0.14), style="<|-|>")
    ax.text(0.20, 0.165, "batched, one transaction", fontsize=5.8, color=GREY, va="center")
    box(ax, 0.40, 0.06, 0.27, 0.10, "NOT IMPLEMENTED\nnetwork transport (ROS 2 / Zenoh / DDS)", fc="#f4f4f4", ec="#888", ls=(0, (3, 2)), tc="#666", fs=6.4)
    box(ax, 0.70, 0.06, 0.27, 0.10, "NOT DEPLOYED\nRaspberry Pi / Jetson, one host per robot", fc="#f4f4f4", ec="#888", ls=(0, (3, 2)), tc="#666", fs=6.4)
    ax.text(0.5, 0.005, "Solid = implemented and tested.  Dashed = design target only, not present in the repository.", ha="center", fontsize=6.4, color=GREY)
    save(fig, "f2_arch")


# ---------------------------------------------------------------- Fig 3: control cycle
def fig_cycle():
    fig, ax = plt.subplots(figsize=(6.9, 4.6))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    steps = ["1  Drain inbox: update peer table\n    (POSE, claims, awards, alerts)", "2  Expire peers silent > 8 ticks\n    (claims lapse; hand back their task)",
             "3  Close auctions with no reply after 3 ticks", "4  If Blocked (no route): retry planning every 3 ticks", "5  If docked and holding a task: undock"]
    y = 0.89
    for i, t in enumerate(steps):
        box(ax, 0.03, y - 0.07, 0.40, 0.085, t, fc="#f1ede6", fs=6.4, align="left")
        if i:
            arrow(ax, (0.23, y + 0.03), (0.23, y + 0.015), lw=0.8)
        y -= 0.115
    box(ax, 0.03, 0.30 - 0.07, 0.40, 0.085, "7  Docked at the charge bay: charge 1.5 %/tick\n8  Sync leases; 9  Publish POSE (heartbeat + claims)", fc="#f1ede6", fs=6.4, align="left")
    box(ax, 0.03, 0.10 - 0.05, 0.40, 0.085, "Repeat every control period (0.6 s), asynchronously\nper robot (phase-shifted in live mode)", fc="#ffffff", ec=GREY, ls=(0, (3, 2)), fs=6.2, tc=GREY, align="left")
    arrow(ax, (0.23, 0.315), (0.23, 0.245), lw=0.8); arrow(ax, (0.23, 0.245), (0.23, 0.2), lw=0.8)
    # 6 Move detail
    box(ax, 0.52, 0.76, 0.45, 0.13, "6  Move (one step along the planned path)\nNeeded nodes = next 2 path nodes", fc="#fbe3d6", ec=ACC, fs=6.8, bold=True)
    arrow(ax, (0.43, 0.29), (0.52, 0.29), lw=0.0)
    ax.annotate("", xy=(0.52, 0.80), xytext=(0.43, 0.48), arrowprops=dict(arrowstyle="-|>", color=INK, lw=0.8, connectionstyle="arc3,rad=-0.15"))
    tests = ["Does a live peer claim my own node?", "Is a needed node claimed by a live peer?", "Is a needed node a silent robot's position?", "C-14: is a closer / higher-rank peer heading there?"]
    yy = 0.66
    for t in tests:
        box(ax, 0.52, yy - 0.055, 0.45, 0.06, t, fc="#ffffff", fs=6.3)
        yy -= 0.085
    box(ax, 0.52, 0.245, 0.21, 0.09, "ALL NO\nclaim nodes (all-or-nothing)\nadvance by speed/tick", fc="#e6f2e3", ec="#2e7d32", fs=6.0)
    box(ax, 0.76, 0.245, 0.21, 0.09, "ANY YES\nwait (Yielding)\ncount blocked ticks", fc="#fdeaea", ec="#b02a2a", fs=6.0)
    box(ax, 0.52, 0.06, 0.45, 0.14, "After 3 blocked ticks: weigh a detour (extra travel <= 14 ticks, cooldown 20)\nAfter 12: find wait-for cycle, victim parks (BFS <= 4 hops)\nSilent robot blocks for its remaining lease", fc="#f8f6f2", ec=GREY, fs=6.0, align="left")
    arrow(ax, (0.865, 0.245), (0.865, 0.205), lw=0.8)
    save(fig, "f3_cycle")


# ---------------------------------------------------------------- Fig 4: auction sequence
def fig_auction():
    A = D["auction"]; msgs = A["msgs"]
    bids = {m["from"]: m["body"]["score"] for m in msgs if m["type"] == "TASK_BID"}
    task = next(m for m in msgs if m["type"] == "TASK_ANNOUNCE")["body"]["task"]
    win = next(m for m in msgs if m["type"] == "TASK_AWARD")
    lanes = ["WMS", "AMR-01", "AMR-02", "AMR-03"]
    x = {l: 0.12 + i * 0.25 for i, l in enumerate(lanes)}
    fig, ax = plt.subplots(figsize=(6.9, 3.9))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    caps = {r["id"]: r for r in A["robots"]}
    for l in lanes:
        sub = "" if l == "WMS" else f"\n{caps[l]['kg']:.0f} kg, {caps[l]['bat']:.0f} %, at {caps[l]['home']}"
        box(ax, x[l] - 0.105, 0.86, 0.21, 0.11, l + sub, fc="#f1ede6" if l == "WMS" else "#ffffff", ec=RC.get(l, INK), lw=1.4, fs=6.4, bold=False)
        ax.plot([x[l], x[l]], [0.86, 0.06], color="#999", lw=0.7, ls=(0, (2, 2)))
    y = 0.78
    for l in lanes[1:]:
        arrow(ax, (x["WMS"], y), (x[l], y), lw=0.9)
        y -= 0.001
    ax.text((x["WMS"] + x["AMR-03"]) / 2, 0.795, f"1  TASK_ANNOUNCE  {task['id']}: {task['pickup']} -> {task['destination']}, {task['payload_kg']:.0f} kg, priority {task['priority']}", fontsize=6.4, ha="center", va="bottom")
    ax.text(0.99, 0.665, "2  every robot answers with a bid\n    computed locally from its own\n    battery, payload limit and A*\n    distance to the pickup", fontsize=6.0, ha="right", va="top", color=GREY)
    y = 0.62
    for l in lanes[1:]:
        for m in lanes[1:]:
            if m != l:
                arrow(ax, (x[l], y), (x[m], y), lw=0.5, color="#888", style="-|>")
        ax.text(x[l] + 0.012, y + 0.008, f"BID {bids[l]:.1f}", fontsize=6.4, color=RC[l], fontweight="bold", va="bottom")
        y -= 0.085
    y = 0.31
    ax.text(0.02, y + 0.012, "3  after all live peers have replied (or 3 ticks), every robot picks max(score, id)", fontsize=6.4, va="bottom")
    y -= 0.075
    for m in lanes[1:]:
        if m != win["from"]:
            arrow(ax, (x[win["from"]], y), (x[m], y), lw=0.9, color=RC[win["from"]])
    ax.text(x[win["from"]] + 0.012, y + 0.008, f"TASK_AWARD -> {win['from']} (score {win['body']['score']:.1f})", fontsize=6.4, color=RC[win["from"]], fontweight="bold", va="bottom")
    ax.text(x[win["from"]] + 0.012, y - 0.09, "only the winner acts; the task board\nrecords it because it hears the award", fontsize=6.0, color=GREY, va="top")
    ax.text(0.02, 0.02, "Source: message trace captured from the running simulation (PeerBus tap), tick 0. Only the winner needs a free vehicle;\nAMR-02 (350 kg limit) could not carry 320 kg with margin, so its low score was expected.", fontsize=5.6, color=GREY, va="bottom")
    save(fig, "f4_auction")


# ---------------------------------------------------------------- Fig 5: head-on space-time
def fig_headon():
    fig, axs = plt.subplots(1, 2, figsize=(6.9, 3.5), sharey=True, sharex=True)
    for ax, key, title in zip(axs, ("decentralized", "stop_and_wait"), ("Decentralised mesh", "Stop-and-wait baseline")):
        h = D["head_on"][key]; rows = h["rows"]
        t = [r["t"] * PERIOD for r in rows]
        ax.axhspan(470, 530, color="#fbe3d6", zorder=0)
        ax.text(1, 500, "C-14", fontsize=6.5, color=ACC, va="center", fontweight="bold")
        for rid in ("AMR-01", "AMR-02", "AMR-03"):
            xs = [r["a"][rid]["x"] for r in rows]
            if max(xs) - min(xs) < 5:
                continue
            ax.plot(t, xs, color=RC[rid], lw=1.2, label=rid, zorder=3)
            hold = [(tt, xx) for tt, xx, r in zip(t, xs, rows) if r["a"][rid]["c14"]]
            if hold:
                ax.plot([p[0] for p in hold], [p[1] for p in hold], ls="", marker="o", ms=1.8, color="#111", zorder=4)
            yl = [(tt, xx) for tt, xx, r in zip(t, xs, rows) if r["a"][rid]["s"] == "Yielding"]
            if yl:
                ax.plot([p[0] for p in yl], [p[1] for p in yl], ls="", marker="s", ms=2.2, mfc="none", mec=RC[rid], mew=0.6, zorder=4)
        ax.set_title(f"{title}: all done at {h['done'] * PERIOD:.1f} s", fontsize=8)
        ax.set_xlabel("time (s)")
        ax.grid(alpha=0.25, lw=0.4)
    axs[0].set_ylabel("position along the aisle, x (map units)")
    axs[0].plot([], [], "o", ms=2, color="#111", label="holds C-14 claim")
    axs[0].plot([], [], "s", ms=3, mfc="none", mec="#555", label="yielding (waiting)")
    axs[0].legend(fontsize=6, frameon=False, loc="lower right")
    save(fig, "f5_headon")


# ---------------------------------------------------------------- Fig 6: deadlock snapshots
def fig_deadlock():
    g = D["graph"]; N = {n["id"]: n for n in g["nodes"]}
    dl = D["deadlock"]; rows = {r["t"]: r for r in dl["rows"]}
    sub = ["RACK A-02", "RACK B-02", "INT-W2", "INT-W1", "C-14", "DOCK-W", "BYPASS-WM"]
    ticks = [(20, "t = 12.0 s\nboth robots arrive at the\nswap, each blocks the other"), (30, "t = 18.0 s\nwaiting: cycle A-02 <-> B-02"), (46, "t = 27.6 s\nvictim (AMR-03) has parked\nat JCT-W1; AMR-01 passes"), (66, "t = 39.6 s\nswap done; AMR-03 resumes")]
    fig, axs = plt.subplots(1, 4, figsize=(6.9, 2.5))
    for ax, (tk, cap) in zip(axs, ticks):
        r = rows.get(tk) or dl["rows"][min(len(dl["rows"]) - 1, tk - 1)]
        for a, b in g["edges"]:
            if a in sub and b in sub:
                ax.plot([N[a]["x"], N[b]["x"]], [N[a]["y"], N[b]["y"]], color="#b8b3ab", lw=1.0, zorder=1)
        for n in sub:
            k = N[n]["kind"]
            if k == "rack":
                ax.add_patch(Rectangle((N[n]["x"] - 20, N[n]["y"] - 11), 40, 22, fc="#f1ede6", ec="#7d766c", lw=0.7, zorder=2))
                ax.text(N[n]["x"], N[n]["y"], n.replace("RACK ", ""), fontsize=5, ha="center", va="center", zorder=3)
            else:
                ax.add_patch(Circle((N[n]["x"], N[n]["y"]), 8, fc="#fff", ec="#555", lw=0.7, zorder=2))
                ax.text(N[n]["x"], N[n]["y"] + 22, n.replace("INT-", "").replace("BYPASS-", "B-"), fontsize=4.8, ha="center", va="top", color="#444")
        for rid, s in r["a"].items():
            if 250 < s["x"] < 520 and 120 < s["y"] < 420:
                ax.plot(s["x"], s["y"], "o", ms=8, color=RC[rid], mec="#111", mew=0.6, zorder=6)
                ax.text(s["x"], s["y"], rid[-1], fontsize=5.5, ha="center", va="center", color="white", zorder=7, fontweight="bold")
        ax.set_xlim(240, 540); ax.set_ylim(380, 120); ax.set_aspect("equal"); ax.axis("off")
        ax.set_title(cap, fontsize=5.6, fontweight="normal", loc="left", va="top", y=-0.02)
    fig.subplots_adjust(wspace=0.08)
    save(fig, "f6_deadlock")


# ---------------------------------------------------------------- Fig 7: dropout timeline
def fig_dropout():
    dr = D["dropout"]; rows = dr["rows"]; S = PERIOD
    fig, ax = plt.subplots(figsize=(6.9, 2.9))
    lanes = ["AMR-01 (victim)", "AMR-02", "AMR-03", "task T0 owner"]
    ypos = {l: 3 - i for i, l in enumerate(lanes)}
    ax.set_xlim(0, 60); ax.set_ylim(-0.7, 3.7)
    ax.set_yticks(list(ypos.values())); ax.set_yticklabels(lanes, fontsize=7)
    ax.barh(ypos["AMR-01 (victim)"], (38 - 8) * S, left=8 * S, height=0.32, color="#e9c9bd", ec=RC["AMR-01"], lw=0.8)
    ax.text(8 * S + 0.3, ypos["AMR-01 (victim)"], "radio silent 18 s: safety stop, sends nothing", fontsize=6.4, va="center")
    ax.barh(ypos["AMR-02"], (17 - 8) * S, left=8 * S, height=0.32, color="#eeeeee", ec="#999", lw=0.6)
    ax.barh(ypos["AMR-03"], (17 - 8) * S, left=8 * S, height=0.32, color="#eeeeee", ec="#999", lw=0.6)
    ax.text(8 * S + 0.25, ypos["AMR-02"], "still honour its claims", fontsize=6, va="center", color="#555")
    ax.text(8 * S + 0.25, ypos["AMR-03"], "(heartbeat timeout 8 ticks)", fontsize=6, va="center", color="#555")
    ev = [(17, "AMR-02", "both peers detect the silence\n(t = 10.2 s); claims lapse"), (17, "task T0 owner", "ORPHAN published by lowest-id\nsurvivor; T0 back in queue"), (18, "task T0 owner", "")]
    ax.plot([17 * S], [ypos["AMR-02"]], "v", color=RC["AMR-02"], ms=6); ax.plot([17 * S], [ypos["AMR-03"]], "v", color=RC["AMR-03"], ms=6)
    ax.text(17.4 * S, ypos["AMR-02"] + 0.06, "detect, t = 10.2 s; claims lapse", fontsize=6.2, va="bottom")
    ax.barh(ypos["task T0 owner"], (17 - 8) * S, left=8 * S, height=0.32, color="#e9c9bd", ec=RC["AMR-01"], lw=0.8); ax.text(8 * S + 0.25, ypos["task T0 owner"], "AMR-01 (silent)", fontsize=6.2, va="center")
    ax.barh(ypos["task T0 owner"], (17 - 17) * S + 0.6, left=17 * S, height=0.32, color="#dddddd", ec="#777", lw=0.6)
    ax.annotate("queued 0.6 s, then won\nby AMR-02 (re-auction)", (17.6 * S, ypos["task T0 owner"] - 0.18), xytext=(20, ypos["task T0 owner"] - 0.55), fontsize=6.2, arrowprops=dict(arrowstyle="-", lw=0.6, color="#555"))
    ax.barh(ypos["task T0 owner"], (87 - 18) * S, left=18 * S, height=0.32, color="#f3e2a8", ec=RC["AMR-02"], lw=0.8)
    ax.text(21, ypos["task T0 owner"] + 0.02, "AMR-02 carries T0 to the dock", fontsize=6.4, va="center")
    ax.plot([38 * S], [ypos["AMR-01 (victim)"]], "o", color=RC["AMR-01"], ms=5)
    ax.text(38 * S + 0.3, ypos["AMR-01 (victim)"] + 0.2, "rejoins (t = 22.8 s),\nrequests SYNC; task stays with AMR-02", fontsize=6.0, va="bottom")
    ax.plot([87 * S], [ypos["task T0 owner"]], "*", color="#111", ms=8)
    ax.text(87 * S - 0.4, ypos["task T0 owner"] + 0.28, "T0 delivered, t = 52.2 s", fontsize=6.2, ha="right")
    ax.set_xlabel("time (s), 0.6 s per control tick")
    ax.grid(axis="x", alpha=0.25, lw=0.4)
    save(fig, "f7_dropout")


# ---------------------------------------------------------------- Fig 8: state machine
def fig_states():
    fig, ax = plt.subplots(figsize=(6.9, 3.7))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    pos = {"Idle": (0.09, 0.78), "Charging": (0.09, 0.28), "Task handoff": (0.40, 0.78), "Moving": (0.70, 0.78), "Yielding": (0.70, 0.42), "Rerouting": (0.40, 0.42), "Blocked": (0.40, 0.08)}
    fill = {"Idle": "#f1ede6", "Charging": "#e4f1e0", "Task handoff": "#f5f0d8", "Moving": "#e6f2e3", "Yielding": "#fdf1d6", "Rerouting": "#e3edf7", "Blocked": "#fdeaea"}
    w, h = 0.16, 0.10
    for k, (x, y) in pos.items():
        box(ax, x - w / 2, y - h / 2, w, h, k, fc=fill[k], fs=7.5, bold=True)
    def ar(a, b, t, off=(0, 0.012), rad=0.0, ha="center"):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        dx, dy = x2 - x1, y2 - y1
        sx = 1 if dx > 0 else -1 if dx < 0 else 0; sy = 1 if dy > 0 else -1 if dy < 0 else 0
        p = (x1 + sx * w / 2, y1 + (sy * h / 2 if dx == 0 else 0)); q = (x2 - sx * w / 2, y2 - (sy * h / 2 if dx == 0 else 0))
        ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=8, color=INK, lw=0.9, connectionstyle=f"arc3,rad={rad}"))
        ax.text((p[0] + q[0]) / 2 + off[0], (p[1] + q[1]) / 2 + off[1], t, fontsize=5.8, ha=ha, va="bottom", color="#333")
    ar("Idle", "Task handoff", "wins auction")
    ar("Task handoff", "Moving", "first step")
    ar("Moving", "Yielding", "claim refused", off=(0.075, -0.05))
    ar("Yielding", "Moving", "claims free", off=(-0.075, 0.0), rad=-0.35)
    ar("Yielding", "Rerouting", "detour / side-step", off=(0, 0.012))
    ar("Rerouting", "Moving", "path applied", off=(-0.06, 0.04), rad=0.0)
    ar("Rerouting", "Blocked", "no route", off=(0.075, -0.05))
    ar("Blocked", "Rerouting", "route found (every 3 ticks)", off=(-0.095, 0.0), rad=-0.5)
    ar("Idle", "Charging", "battery < 30 %:\nto charge bay", off=(0.075, -0.05))
    ax.text(0.09, 0.62, "", fontsize=6)
    ax.text(0.70, 0.945, "Moving -> Idle when a task finishes and the robot docks at home", fontsize=6.1, color=GREY, ha="center")
    ax.text(0.70, 0.12, "Radio dropout: status becomes Blocked (\"offline\") and the robot stays put;\non rejoin it clears its old task and returns to Idle", fontsize=6.1, color=GREY, ha="center", va="center")
    ax.text(0.09, 0.08, "Status strings are exactly those\nreported by the dashboard (RobotStatus).", fontsize=6.1, color=GREY, ha="left", va="center")
    save(fig, "f8_states")


# ---------------------------------------------------------------- Fig 9: benchmark
def fig_bench():
    det = D["benchmark"]["detail"]
    sw = [r["sw_mission"] * PERIOD for r in det]; dc = [r["dec_mission"] * PERIOD for r in det]; red = [r["red"] for r in det]
    fig = plt.figure(figsize=(6.9, 3.0))
    ax1 = fig.add_axes([0.06, 0.16, 0.27, 0.72]); ax2 = fig.add_axes([0.40, 0.16, 0.27, 0.72]); ax3 = fig.add_axes([0.76, 0.16, 0.22, 0.72])
    slower = [d > s for s, d in zip(sw, dc)]
    ax1.scatter([s for s, f in zip(sw, slower) if not f], [d for d, f in zip(dc, slower) if not f], s=7, c="#2e7d32", label="mesh faster")
    ax1.scatter([s for s, f in zip(sw, slower) if f], [d for d, f in zip(dc, slower) if f], s=9, c="#b02a2a", marker="x", label=f"mesh slower ({sum(slower)})")
    lim = max(sw + dc) * 1.05
    ax1.plot([0, lim], [0, lim], color="#888", lw=0.7, ls="--"); ax1.set_xlim(0, lim); ax1.set_ylim(0, lim)
    ax1.set_xlabel("stop-and-wait: sum of mission times (s)"); ax1.set_ylabel("mesh (s)"); ax1.set_title("(a) 100 paired runs", fontsize=8)
    ax1.legend(fontsize=5.8, frameon=False, loc="upper left")
    ax2.hist(red, bins=18, color="#c9c3ba", ec="#555", lw=0.5)
    m = D["benchmark"]["reduction_pct"]; med = D["benchmark"]["median_reduction_pct"]
    for v, lab, c, ls in ((m, f"mean {m}%", ACC, "-"), (med, f"median {med}%", "#1f5fa8", "-"), (20, "20% target", "#333", "--"), (0, "", "#b02a2a", ":")):
        ax2.axvline(v, color=c, ls=ls, lw=1.0)
    ax2.text(m + 1, ax2.get_ylim()[1] * 0.93, f"mean {m}%", fontsize=6, color=ACC, ha="left")
    ax2.text(med - 1, ax2.get_ylim()[1] * 0.80, f"median {med}%", fontsize=6, color="#1f5fa8", ha="right")
    ax2.set_xlabel("per-run reduction in mission time (%)"); ax2.set_ylabel("runs"); ax2.set_title("(b) spread of the saving", fontsize=8)
    B = D["benchmark"]
    labels = ["mission\ntime", "makespan", "waiting\n(robot-s)"]
    bw = [B["stop_and_wait_mission_seconds"], B["stop_and_wait_makespan_seconds"], sum(r["sw_wait"] for r in det) / len(det) * PERIOD]
    dw = [B["decentralized_mission_seconds"], B["decentralized_makespan_seconds"], sum(r["dec_wait"] for r in det) / len(det) * PERIOD]
    idx = range(3)
    ax3.bar([i - 0.19 for i in idx], bw, 0.36, color="#9b948a", label="stop-and-wait"); ax3.bar([i + 0.19 for i in idx], dw, 0.36, color=ACC, label="mesh")
    for i, (a, b) in enumerate(zip(bw, dw)):
        ax3.text(i + 0.19, b + 8, f"-{100 * (a - b) / a:.0f}%", fontsize=6, ha="center", color=ACC, fontweight="bold")
    ax3.set_xticks(list(idx)); ax3.set_xticklabels(labels, fontsize=6.5); ax3.set_ylabel("seconds, mean per run"); ax3.set_title("(c) means", fontsize=8); ax3.legend(fontsize=5.8, frameon=False, loc="upper right")
    save(fig, "f9_bench")


# ---------------------------------------------------------------- Fig 10: scaling
def fig_scaling():
    S = D["scaling"][:3]
    fig, axs = plt.subplots(1, 3, figsize=(6.9, 2.6))
    n = [s["robots"] for s in S]
    axs[0].bar([str(i) for i in n], [s["tasks"] for s in S], color="#d9d4cb", ec="#777", label="issued")
    axs[0].bar([str(i) for i in n], [s["done"] for s in S], color=ACC, width=0.5, label="completed")
    axs[0].set_title("(a) tasks in 2 500 ticks", fontsize=8); axs[0].set_xlabel("robots"); axs[0].legend(fontsize=6, frameon=False, loc="upper left")
    for i, s in enumerate(S):
        axs[0].text(i, s["tasks"] + 2, f"{s['done']}/{s['tasks']}", ha="center", fontsize=6.5)
    yl = [s["yield"] / max(1, s["done"]) for s in S]
    axs[1].bar([str(i) for i in n], yl, color="#c48a00")
    for i, v in enumerate(yl):
        axs[1].text(i, v + 0.4, f"{v:.1f}", ha="center", fontsize=6.5)
    axs[1].set_title("(b) yield events per completed task", fontsize=8); axs[1].set_xlabel("robots")
    axs[2].plot(n, [s["tick_ms_mean"] for s in S], "o-", color="#1f5fa8", label="mean")
    axs[2].plot(n, [s["tick_ms_p95"] for s in S], "s--", color="#1f5fa8", label="p95", mfc="none")
    axs[2].set_title("(c) coordination cost per tick (ms)", fontsize=8); axs[2].set_xlabel("robots"); axs[2].legend(fontsize=6, frameon=False); axs[2].set_xticks(n); axs[2].grid(alpha=0.25, lw=0.4)
    fig.subplots_adjust(wspace=0.35)
    save(fig, "f10_scaling")


# ---------------------------------------------------------------- Fig 11: evidence map (implemented vs claimed)
def fig_evidence():
    rows = [("Robot agents with private state", 3), ("Contract-Net style auction", 3), ("Node claims + soft-state leases", 3), ("Deadlock cycle detection + recovery", 3),
            ("A* re-planning on blockage", 3), ("Dropout detection + task hand-back", 3), ("Live dashboard + message view", 3), ("Measured comparison vs stop-and-wait", 2),
            ("Message delay / loss handling", 0), ("Space-Time A*", 0), ("ROS 2 / Zenoh transport", 0), ("Edge hardware (Pi / Jetson)", 0), ("Safety certification (ISO 3691-4)", 0)]
    colr = {3: "#2e7d32", 2: "#c48a00", 0: "#b02a2a"}; lab = {3: "implemented + tested", 2: "implemented, self-defined baseline", 0: "not present"}
    fig, ax = plt.subplots(figsize=(6.9, 3.4))
    for i, (t, v) in enumerate(rows):
        y = len(rows) - i
        ax.add_patch(Rectangle((0, y - 0.35), 1, 0.7, fc=colr[v], ec="none", alpha=0.9))
        ax.text(1.12, y, t, va="center", fontsize=7.4)
        ax.text(6.9, y, lab[v], va="center", fontsize=6.6, color=colr[v], fontweight="bold")
    ax.set_xlim(0, 9.6); ax.set_ylim(0.3, len(rows) + 0.8); ax.axis("off")
    save(fig, "f11_status")


if __name__ == "__main__":
    for f in (fig_map, fig_arch, fig_cycle, fig_auction, fig_headon, fig_deadlock, fig_dropout, fig_states, fig_bench, fig_scaling):
        f(); print("ok", f.__name__)
