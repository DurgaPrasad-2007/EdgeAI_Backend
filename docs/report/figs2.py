from figs import *  # noqa: F401,F403  (D, helpers, palette)
import figs


# ------------------------------------------------------------ Fig 1 map (annotations moved clear of racks)
def fig_map():
    g = D["graph"]; N = {n["id"]: n for n in g["nodes"]}
    fig, ax = plt.subplots(figsize=(6.9, 4.5))
    for a, b in g["edges"]:
        ax.plot([N[a]["x"], N[b]["x"]], [N[a]["y"], N[b]["y"]], color="#b8b3ab", lw=1.1, zorder=1)
    for e in (("BYPASS-WM", "DETOUR-SW"), ("DETOUR-SW", "DETOUR-S"), ("DETOUR-S", "DETOUR-SE"), ("DETOUR-SE", "BYPASS-EM")):
        ax.plot([N[e[0]]["x"], N[e[1]]["x"]], [N[e[0]]["y"], N[e[1]]["y"]], color="#3b7fbf", lw=1.6, ls=(0, (4, 2)), zorder=2)
    for n in g["nodes"]:
        k, x, y = n["kind"], n["x"], n["y"]
        if k == "rack":
            ax.add_patch(Rectangle((x - 22, y - 11), 44, 22, fc="#f1ede6", ec="#7d766c", lw=0.8, zorder=3))
        elif k in ("dock", "charge"):
            ax.add_patch(Rectangle((x - 30, y - 13), 60, 26, fc="#dfe9f3" if k == "dock" else "#e4f1e0", ec="#2a5f94" if k == "dock" else "#2e7d32", lw=0.9, zorder=3))
        elif k == "corridor":
            ax.add_patch(Circle((x, y), 21, fc="#fbe3d6", ec=ACC, lw=1.6, zorder=3))
        else:
            ax.add_patch(Circle((x, y), 7, fc="#ffffff", ec="#555", lw=0.8, zorder=3))
        lab = n["id"].replace("RACK ", "").replace("INT-", "").replace("DETOUR-", "D-").replace("BYPASS-", "B-")
        if k in ("rack", "dock", "charge", "corridor"):
            ax.text(x, y, lab, ha="center", va="center", fontsize=5.6, zorder=4, color=INK, fontweight="bold" if k == "corridor" else "normal")
        else:
            dy = 13 if n["id"] in ("DETOUR-S", "INT-S2") else -13
            va = "top" if dy > 0 else "bottom"
            if n["id"] == "INT-S2":
                ax.text(x + 12, y + 2, lab, ha="left", va="center", fontsize=5.0, color="#444", zorder=4)
            elif n["id"] == "DETOUR-S":
                ax.text(x + 12, y + 2, lab, ha="left", va="center", fontsize=5.0, color="#444", zorder=4)
            else:
                ax.text(x, y + dy * -1 if False else y - 13, lab, ha="center", va="bottom", fontsize=5.0, color="#444", zorder=4)
    b = N["AISLE-B07"]
    ax.add_patch(Circle((b["x"], b["y"]), 12, fc="none", ec="#c00000", lw=1.2, ls=(0, (2, 1.5)), zorder=5))
    ax.annotate("AISLE-B07 (operator-blockable)", (b["x"] - 8, b["y"] + 12), xytext=(560, 535), fontsize=6.3, color="#c00000", arrowprops=dict(arrowstyle="-", color="#c00000", lw=0.7))
    ax.annotate("C-14: the only mutex zone\n(single-lane corridor)", (N["C-14"]["x"] - 14, N["C-14"]["y"] - 16), xytext=(600, 62), fontsize=6.3, color=ACC, arrowprops=dict(arrowstyle="-", color=ACC, lw=0.7))
    for r in g["robots"]:
        h = N[r["home"]]
        ax.plot(h["x"], h["y"] + 21, marker="v", color=RC[r["id"]], ms=6, zorder=6)
        ax.text(h["x"], h["y"] + 32, r["id"], fontsize=5.8, ha="center", va="top", color=RC[r["id"]], fontweight="bold")
    ax.plot([], [], color="#3b7fbf", ls=(0, (4, 2)), lw=1.6, label="perimeter detour lane around B-07")
    ax.plot([], [], marker="v", color="#444", ls="", ms=5, label="robot home (docked start)")
    ax.legend(loc="lower left", fontsize=6.2, frameon=False, bbox_to_anchor=(0.0, -0.05))
    ax.set_xlim(40, 960); ax.set_ylim(585, 45); ax.set_aspect("equal"); ax.axis("off")
    save(fig, "f1_map")


# ------------------------------------------------------------ Fig 2 architecture
def fig_arch():
    fig, ax = plt.subplots(figsize=(6.9, 5.0))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    box(ax, 0.03, 0.90, 0.30, 0.08, "Dashboard (Next.js)\nread-only observer", fc="#eef3f8", fs=6.8)
    arrow(ax, (0.18, 0.90), (0.18, 0.855), style="<|-|>")
    ax.text(0.20, 0.877, "REST / WebSocket", fontsize=6, color=GREY, va="center")
    ax.add_patch(FancyBboxPatch((0.03, 0.20), 0.94, 0.655, boxstyle="round,pad=0,rounding_size=0.015", fc="#fbfaf8", ec=INK, lw=1.2))
    ax.text(0.045, 0.835, "ONE Python process (FastAPI / uvicorn)", fontsize=7.2, fontweight="bold", va="top")
    box(ax, 0.06, 0.64, 0.26, 0.135, "Gateway\nREST + WebSocket, JWT,\nwrite-behind persistence", fc="#f1ede6", fs=6.4)
    box(ax, 0.37, 0.64, 0.26, 0.135, "Task board (\"WMS\")\nannounces queued tasks,\nhears awards and status", fc="#f1ede6", fs=6.4)
    box(ax, 0.68, 0.64, 0.26, 0.135, "World (ground truth)\npositions, blocked aisles,\nproximity monitor", fc="#f1ede6", fs=6.4)
    box(ax, 0.06, 0.44, 0.88, 0.075, "PeerBus: in-process publish/subscribe\nbroadcast + direct messages, one inbox per robot, radio-silence switch", fc="#fbe3d6", ec=ACC, lw=1.3, fs=6.6, bold=True)
    for i, rid in enumerate(("AMR-01", "AMR-02", "AMR-03")):
        x = 0.06 + i * 0.30
        box(ax, x, 0.225, 0.28, 0.16, f"RobotAgent {rid}\nown asyncio control loop\nprivate: pose, path, claims,\npeer table, auctions, task", fc="#ffffff", ec=RC[rid], lw=1.5, fs=6.2)
        arrow(ax, (x + 0.14, 0.385), (x + 0.14, 0.44), style="<|-|>", lw=0.9)
    arrow(ax, (0.19, 0.64), (0.19, 0.515), style="<|-|>")
    arrow(ax, (0.50, 0.64), (0.50, 0.515), style="<|-|>")
    arrow(ax, (0.81, 0.64), (0.81, 0.515), style="<|-|>", ls=(0, (3, 2)), color=GREY)
    ax.text(0.83, 0.578, "robots write own pose;\nsense obstacles only", fontsize=5.6, color=GREY, va="center")
    arrow(ax, (0.18, 0.20), (0.18, 0.15), style="<|-|>")
    ax.text(0.20, 0.175, "batched: one transaction, one connection", fontsize=5.8, color=GREY, va="center")
    box(ax, 0.03, 0.05, 0.30, 0.09, "Database\nSQLite or PostgreSQL + pgvector", fc="#f3f0ea", fs=6.4)
    box(ax, 0.38, 0.05, 0.28, 0.10, "NOT IMPLEMENTED\nnetwork transport\n(ROS 2 / Zenoh / DDS)", fc="#f4f4f4", ec="#888", ls=(0, (3, 2)), tc="#666", fs=6.0)
    box(ax, 0.70, 0.05, 0.27, 0.10, "NOT DEPLOYED\none edge computer per robot\n(Raspberry Pi / Jetson)", fc="#f4f4f4", ec="#888", ls=(0, (3, 2)), tc="#666", fs=6.0)
    ax.text(0.5, 0.005, "Solid boxes are implemented and tested. Dashed boxes are design targets that are not in the repository.", ha="center", fontsize=6.0, color=GREY)
    save(fig, "f2_arch")


# ------------------------------------------------------------ Fig 3 control cycle
def fig_cycle():
    fig, ax = plt.subplots(figsize=(6.9, 4.4))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.03, 0.965, "Control cycle of one robot (0.6 s)", fontsize=7.5, fontweight="bold", va="top")
    steps = ["1  Drain inbox and update the peer table", "2  Expire peers silent for more than 8 ticks", "3  Close auctions still open after 3 ticks",
             "4  If Blocked (no route): retry every 3rd tick", "5  If docked and holding a task: undock", "6  MOVE one step (checks on the right)",
             "7  If docked at the charge bay: charge 1.5 %/tick", "8  Sync leases;  9  publish POSE (heartbeat)"]
    y0, hh, gap = 0.88, 0.078, 0.028
    for i, t in enumerate(steps):
        y = y0 - i * (hh + gap)
        hot = i == 5
        box(ax, 0.03, y - hh, 0.42, hh, t, fc="#fbe3d6" if hot else "#f1ede6", ec=ACC if hot else INK, fs=6.3, align="left", bold=hot)
        if i:
            arrow(ax, (0.24, y + gap + 0.004), (0.24, y + 0.002), lw=0.8)
    ax.add_patch(FancyArrowPatch((0.45, y0 - 5 * (hh + gap) - hh / 2), (0.52, y0 - 5 * (hh + gap) - hh / 2), arrowstyle="-|>", mutation_scale=8, color=ACC, lw=1.0))
    ax.text(0.53, 0.955 - 0.0, "Step 6 in detail", fontsize=7, fontweight="bold", va="top", color=ACC)
    checks = ["a  Does a live peer already claim MY node?", "b  Is a next-2 path node claimed by a live peer?", "c  Is a needed node where a silent robot stopped?", "d  C-14: a closer / higher-rank peer heading there?"]
    yy = 0.885
    for t in checks:
        box(ax, 0.52, yy - 0.062, 0.45, 0.062, t, fc="#ffffff", fs=6.0, align="left")
        yy -= 0.085
    ax.text(0.745, yy + 0.01, "any YES: wait      all NO: go", fontsize=6.2, ha="center", color=GREY, va="top")
    box(ax, 0.52, 0.375, 0.215, 0.105, "all NO\nclaim next 2 nodes at once\n(all-or-nothing), advance", fc="#e6f2e3", ec="#2e7d32", fs=5.9)
    box(ax, 0.755, 0.375, 0.215, 0.105, "any YES\nstatus Yielding,\nblocked-tick counter +1", fc="#fdeaea", ec="#b02a2a", fs=5.9)
    arrow(ax, (0.8625, 0.375), (0.8625, 0.325), lw=0.8)
    box(ax, 0.52, 0.045, 0.45, 0.275, "While the counter keeps growing:\n\n3 ticks   weigh a detour: take it if the extra\n               travel is at most 14 ticks (cooldown 20)\n12 ticks  look for a wait-for cycle; if one exists,\n               the lowest-rank member parks aside\n               (BFS, at most 4 hops, then holds 15+ ticks)\nA silent robot's spot stays blocked (\"ghost\")", fc="#f8f6f2", ec=GREY, fs=5.9, align="left")
    save(fig, "f3_cycle")


# ------------------------------------------------------------ Fig 4 auction sequence
def fig_auction():
    A = D["auction"]; msgs = A["msgs"]
    bids = {m["from"]: m["body"]["score"] for m in msgs if m["type"] == "TASK_BID"}
    task = next(m for m in msgs if m["type"] == "TASK_ANNOUNCE")["body"]["task"]
    win = next(m for m in msgs if m["type"] == "TASK_AWARD")
    lanes = ["WMS", "AMR-01", "AMR-02", "AMR-03"]
    x = {l: 0.13 + i * 0.25 for i, l in enumerate(lanes)}
    caps = {r["id"]: r for r in A["robots"]}
    fig, ax = plt.subplots(figsize=(6.9, 4.2))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    for l in lanes:
        sub = "task board" if l == "WMS" else f"{caps[l]['kg']:.0f} kg limit, {caps[l]['bat']:.0f} % battery\nat {caps[l]['home']}"
        box(ax, x[l] - 0.11, 0.87, 0.22, 0.115, f"{l}\n{sub}", fc="#f1ede6" if l == "WMS" else "#ffffff", ec=RC.get(l, INK), lw=1.4, fs=6.0)
        ax.plot([x[l], x[l]], [0.87, 0.05], color="#999", lw=0.7, ls=(0, (2, 2)))
    ax.text(0.02, 0.815, "1", fontsize=7, fontweight="bold", color=ACC)
    ax.text(0.05, 0.815, f"TASK_ANNOUNCE   {task['id']}: {task['pickup']} to {task['destination']}, {task['payload_kg']:.0f} kg, priority {task['priority']}", fontsize=6.3, va="bottom")
    for l in lanes[1:]:
        arrow(ax, (x["WMS"], 0.80), (x[l], 0.80), lw=0.9)
    ax.text(0.02, 0.715, "2", fontsize=7, fontweight="bold", color=ACC)
    ax.text(0.05, 0.715, "every robot broadcasts a bid it computed itself (own battery, payload limit, A* distance to the pickup)", fontsize=6.3, va="bottom")
    y = 0.665
    for l in lanes[1:]:
        for m in lanes[1:]:
            if m != l:
                arrow(ax, (x[l], y), (x[m], y), lw=0.5, color=RC[l], style="-|>")
        ax.text(x[l] + 0.012, y + 0.008, f"BID {bids[l]:.1f}", fontsize=6.3, color=RC[l], fontweight="bold", va="bottom")
        y -= 0.072
    ax.text(0.02, 0.405, "3", fontsize=7, fontweight="bold", color=ACC)
    ax.text(0.05, 0.405, "once every live peer has answered (or 3 ticks pass) each robot picks max(score, id) on its own", fontsize=6.3, va="bottom")
    y = 0.355
    for m in lanes[1:]:
        if m != win["from"]:
            arrow(ax, (x[win["from"]], y), (x[m], y), lw=1.0, color=RC[win["from"]])
    ax.text(x[win["from"]] + 0.012, y + 0.008, f"TASK_AWARD to {win['from']} (score {win['body']['score']:.1f})", fontsize=6.3, color=RC[win["from"]], fontweight="bold", va="bottom")
    ax.text(0.05, 0.22, "Only the winner acts; the task board learns the result by hearing the award (it does not choose).\nScores come from the fixed weights in the bid equation (Section 5). AMR-01 wins on capacity margin\n(880 kg spare), battery and proximity to RACK A-02; AMR-02 has only 30 kg spare, AMR-03 starts far away.", fontsize=5.9, color=GREY, va="top")
    ax.text(0.02, 0.02, "Source: message trace recorded by a tap on the PeerBus in the running simulation, tick 0.", fontsize=5.6, color=GREY, va="bottom")
    save(fig, "f4_auction")


# ------------------------------------------------------------ Fig 6 deadlock snapshots (ticks taken from the data)
def fig_deadlock():
    g = D["graph"]; N = {n["id"]: n for n in g["nodes"]}
    dl = D["deadlock"]; rows = dl["rows"]; ev = dl["events"]
    aside_t = next(e["t"] for e in ev if "stepped aside" in e["msg"])
    first_yield = next(e["t"] for e in ev if "yielding" in e["msg"])
    t_park = next(r["t"] for r in rows if r["t"] > aside_t and r["a"]["AMR-03"]["n"] == "INT-W1")
    t_done = next(e["t"] for e in ev if "T0 delivered" in e["msg"])
    ticks = [(first_yield + 8, "waiting: each robot needs the\nnode the other one occupies"), (aside_t, "cycle found; AMR-03 (lowest\nrank) is the victim and moves"), (t_park, "AMR-03 parked at JCT-W1,\nAMR-01 can pass"), (t_done, "AMR-01 delivered; swap complete")]
    sub = ["RACK A-02", "RACK B-02", "INT-W2", "INT-W1", "C-14"]
    names = {"INT-W2": "WP-04", "INT-W1": "JCT-W1", "C-14": "C-14"}
    fig, axs = plt.subplots(1, 4, figsize=(6.9, 2.6))
    for ax, (tk, cap) in zip(axs, ticks):
        r = rows[min(len(rows) - 1, tk - 1)]
        for a, b in g["edges"]:
            if a in sub and b in sub:
                ax.plot([N[a]["x"], N[b]["x"]], [N[a]["y"], N[b]["y"]], color="#b8b3ab", lw=1.0, zorder=1)
        for n in sub:
            k = N[n]["kind"]
            if k == "rack":
                ax.add_patch(Rectangle((N[n]["x"] - 26, N[n]["y"] - 13), 52, 26, fc="#f1ede6", ec="#7d766c", lw=0.7, zorder=2))
                ax.text(N[n]["x"], N[n]["y"] + (26 if n == "RACK B-02" else -26), n.replace("RACK ", "rack "), fontsize=5.2, ha="center", va="top" if n == "RACK B-02" else "bottom")
            else:
                ax.add_patch(Circle((N[n]["x"], N[n]["y"]), 9, fc="#fff", ec="#555", lw=0.7, zorder=2))
                ax.text(N[n]["x"], N[n]["y"] - 13, names[n], fontsize=5.0, ha="center", va="bottom", color="#444")
        for rid, s in r["a"].items():
            if 240 < s["x"] < 540 and 120 < s["y"] < 400:
                ax.plot(s["x"], s["y"], "o", ms=9, color=RC[rid], mec="#111", mew=0.6, zorder=6)
                ax.text(s["x"], s["y"], rid[-1], fontsize=5.8, ha="center", va="center", color="white", zorder=7, fontweight="bold")
        ax.set_xlim(240, 540); ax.set_ylim(400, 110); ax.set_aspect("equal"); ax.axis("off")
        ax.set_title(f"t = {tk * PERIOD:.1f} s", fontsize=6.8, loc="left", pad=1)
        ax.text(240, 408, cap, fontsize=5.6, va="top", ha="left", color="#222")
    fig.subplots_adjust(wspace=0.05)
    save(fig, "f6_deadlock")
    return {"first_yield": first_yield, "aside": aside_t, "park": t_park, "done": t_done}


# ------------------------------------------------------------ Fig 7 dropout timeline
def fig_dropout():
    S = PERIOD
    fig, ax = plt.subplots(figsize=(6.9, 3.3))
    lanes = ["AMR-01 (victim)", "AMR-02", "AMR-03", "owner of task T0"]
    Y = {l: 3 - i for i, l in enumerate(lanes)}
    ax.set_xlim(0, 58); ax.set_ylim(-1.35, 3.7)
    ax.set_yticks(list(Y.values())); ax.set_yticklabels(lanes, fontsize=7)
    h = 0.30
    ax.barh(Y["AMR-01 (victim)"], (38 - 8) * S, left=8 * S, height=h, color="#e9c9bd", ec=RC["AMR-01"], lw=0.8)
    ax.text(8 * S + 0.3, Y["AMR-01 (victim)"] + 0.27, "radio silent 18 s: safety stop, publishes nothing", fontsize=6.2, va="bottom")
    ax.plot([38 * S], [Y["AMR-01 (victim)"]], "o", color=RC["AMR-01"], ms=5)
    ax.text(38 * S + 0.5, Y["AMR-01 (victim)"], "rejoins at 22.8 s, sends SYNC_REQ,\nclears its old task", fontsize=6.0, va="center")
    for l in ("AMR-02", "AMR-03"):
        ax.barh(Y[l], (17 - 8) * S, left=8 * S, height=h, color="#eeeeee", ec="#999", lw=0.6)
        ax.plot([17 * S], [Y[l]], "v", color=RC[l], ms=6)
    ax.text(17 * S + 0.5, Y["AMR-02"] + 0.05, "8 ticks without a heartbeat: both peers mark AMR-01 dead at 10.2 s;\nits claims lapse, its physical spot stays blocked", fontsize=6.0, va="bottom")
    ax.text(8 * S + 0.2, Y["AMR-03"] - 0.33, "still honour its claims", fontsize=5.8, va="top", color="#555")
    ax.barh(Y["owner of task T0"], (17 - 8) * S, left=8 * S, height=h, color="#e9c9bd", ec=RC["AMR-01"], lw=0.8)
    ax.text(8 * S + 0.15, Y["owner of task T0"], "AMR-01", fontsize=5.6, va="center")
    ax.barh(Y["owner of task T0"], 0.6, left=17 * S, height=h, color="#dddddd", ec="#777", lw=0.6)
    ax.barh(Y["owner of task T0"], (87 - 18) * S, left=18 * S, height=h, color="#f3e2a8", ec=RC["AMR-02"], lw=0.8)
    ax.text(21, Y["owner of task T0"], "AMR-02 carries T0 to DOCK-W", fontsize=6.2, va="center")
    ax.annotate("ORPHAN published by the lowest-id survivor;\nT0 re-queued, re-auctioned, won by AMR-02 within 0.6 s", (17.3 * S, Y["owner of task T0"] - 0.16), xytext=(13.2, Y["owner of task T0"] - 0.70), fontsize=5.9, va="top", arrowprops=dict(arrowstyle="-", lw=0.6, color="#555"))
    ax.plot([87 * S], [Y["owner of task T0"]], "*", color="#111", ms=8)
    ax.text(87 * S - 0.5, Y["owner of task T0"] + 0.28, "T0 delivered, 52.2 s", fontsize=6.0, ha="right")
    ax.set_xlabel("time (s); one control tick = 0.6 s"); ax.grid(axis="x", alpha=0.25, lw=0.4)
    save(fig, "f7_dropout")


# ------------------------------------------------------------ Fig 8 state machine
def fig_states():
    fig, ax = plt.subplots(figsize=(6.9, 3.6))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    pos = {"Idle": (0.10, 0.80), "Charging": (0.10, 0.36), "Task handoff": (0.40, 0.80), "Moving": (0.72, 0.80), "Yielding": (0.72, 0.44), "Rerouting": (0.40, 0.44), "Blocked": (0.40, 0.10)}
    fill = {"Idle": "#f1ede6", "Charging": "#e4f1e0", "Task handoff": "#f5f0d8", "Moving": "#e6f2e3", "Yielding": "#fdf1d6", "Rerouting": "#e3edf7", "Blocked": "#fdeaea"}
    w, h = 0.17, 0.10
    for k, (x, y) in pos.items():
        box(ax, x - w / 2, y - h / 2, w, h, k, fc=fill[k], fs=7.2, bold=True)

    def link(p, q, text, tp, rad=0.0, ha="center"):
        ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=8, color=INK, lw=0.9, connectionstyle=f"arc3,rad={rad}"))
        ax.text(tp[0], tp[1], text, fontsize=5.8, ha=ha, va="center", color="#333")
    link((0.185, 0.80), (0.315, 0.80), "wins auction", (0.25, 0.835))
    link((0.485, 0.80), (0.635, 0.80), "first step", (0.56, 0.835))
    link((0.70, 0.75), (0.70, 0.49), "claim refused", (0.685, 0.56), ha="right")
    link((0.75, 0.49), (0.75, 0.75), "claims free", (0.775, 0.62), ha="left")
    link((0.635, 0.44), (0.485, 0.44), "detour or side-step", (0.56, 0.475))
    link((0.44, 0.49), (0.66, 0.75), "path applied", (0.505, 0.635), ha="right")
    link((0.38, 0.39), (0.38, 0.15), "no route", (0.365, 0.27), ha="right")
    link((0.42, 0.15), (0.42, 0.39), "route found\n(retry every 3 ticks)", (0.435, 0.27), ha="left")
    link((0.10, 0.75), (0.10, 0.41), "battery < 30 %\nand idle: go to\nthe charge bay", (0.125, 0.58), ha="left")
    ax.text(0.99, 0.985, "Moving returns to Idle when the task is finished and the robot docks.", fontsize=5.8, color=GREY, ha="right", va="top")
    ax.text(0.99, 0.16, "Radio dropout: status Blocked (offline), robot stops.\nOn rejoin it discards its task and heads home.", fontsize=5.8, color=GREY, ha="right", va="center")
    ax.text(0.01, 0.19, "Labels are the RobotStatus strings\nthe dashboard shows.", fontsize=5.8, color=GREY, ha="left")
    save(fig, "f8_states")


# ------------------------------------------------------------ Fig 10 scaling (shorter titles)
def fig_scaling():
    S = D["scaling"][:3]
    fig, axs = plt.subplots(1, 3, figsize=(6.9, 2.6))
    n = [s["robots"] for s in S]
    axs[0].bar([str(i) for i in n], [s["tasks"] for s in S], color="#d9d4cb", ec="#777", label="issued")
    axs[0].bar([str(i) for i in n], [s["done"] for s in S], color=ACC, width=0.5, label="completed")
    axs[0].set_title("(a) tasks done, 2500 ticks", fontsize=7.5); axs[0].set_xlabel("robots"); axs[0].legend(fontsize=6, frameon=False, loc="upper left")
    for i, s in enumerate(S):
        axs[0].text(i, s["tasks"] + 2, f"{s['done']}/{s['tasks']}", ha="center", fontsize=6.3)
    axs[0].set_ylim(0, 150)
    yl = [s["yield"] / max(1, s["done"]) for s in S]
    axs[1].bar([str(i) for i in n], yl, color="#c48a00")
    for i, v in enumerate(yl):
        axs[1].text(i, v + 0.4, f"{v:.1f}", ha="center", fontsize=6.3)
    axs[1].set_title("(b) waits per completed task", fontsize=7.5); axs[1].set_xlabel("robots")
    axs[2].plot(n, [s["tick_ms_mean"] for s in S], "o-", color="#1f5fa8", label="mean")
    axs[2].plot(n, [s["tick_ms_p95"] for s in S], "s--", color="#1f5fa8", label="p95", mfc="none")
    axs[2].set_title("(c) compute per tick (ms)", fontsize=7.5); axs[2].set_xlabel("robots"); axs[2].legend(fontsize=6, frameon=False, loc="upper left"); axs[2].set_xticks(n); axs[2].grid(alpha=0.25, lw=0.4)
    fig.subplots_adjust(wspace=0.38)
    save(fig, "f10_scaling")


import json
info = None
for f in (fig_map, fig_arch, fig_cycle, fig_auction, fig_dropout, fig_states, fig_scaling):
    f(); print("ok", f.__name__)
info = fig_deadlock(); print("deadlock ticks", info)
json.dump(info, open("dl_info.json", "w"))
