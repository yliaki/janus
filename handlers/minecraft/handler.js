// Minecraft: Java Edition through Prism Launcher (a non-Steam game).
//
// Janus downloads its own portable Prism into ctx.gameDir/prism and installs
// one shared instance ("janus"): the newest Minecraft release that Controlify
// (controller support) supports on Fabric, with Sodium and Mod Menu when
// they exist for it. Prism downloads Minecraft itself. Every player gets
// their own Prism data dir under their profile (ctx.saveRoot/prism): own
// options, Controlify settings and singleplayer worlds; libraries, assets,
// Java, mods and the account file are symlinked to the shared copy.
// The owner plays online on the Microsoft account signed into Janus's Prism,
// everyone else offline under their profile name. Prism runs on the desktop
// and starts the game through ctx.wrapper, which puts it into the tile.
// See NOTES.md "Minecraft Java via Prism Launcher".

Game.id = "minecraft";
Game.name = "Minecraft";
Game.maxPlayers = 8;
Game.launcherStartsGame = true;
Game.isolation = "none";
Game.profileSaves = true;
Game.launchMessage =
    "Player 1 hosts: Singleplayer → open or create a world, then pause → " +
    "World Options → Multiplayer → LAN: ON → Apply Changes.\n" +
    "Everyone else: Multiplayer → the LAN world appears at the bottom → Join Server.\n" +
    "(Before Minecraft 26: pause → Open to LAN.)";

const MODRINTH = "https://api.modrinth.com/v2";
const FABRIC_META = "https://meta.fabricmc.net/v2";
const PRISM_RELEASE = "https://api.github.com/repos/PrismLauncher/PrismLauncher/releases/latest";
const REQUIRED_MODS = ["controlify"];          // controllers
const EXTRA_MODS = ["sodium", "modmenu"];      // added when they exist for the version
const INSTANCE = "janus";
// The user's own Prism Launcher (native / Flatpak): its account can be copied.
const USER_PRISM_ACCOUNTS = [
    "~/.local/share/PrismLauncher/accounts.json",
    "~/.var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/accounts.json",
];

Janus.addOption("memory", {
    label: "Memory per player (GB)",
    type: "choice",
    choices: ["auto", "2", "3", "4", "5", "6", "8"],
    default: "auto",
    description: "Java heap for each player's Minecraft. Auto shares what this " +
                 "computer has between the players (2 to 6 GB each).",
});

// Artwork for Big Picture, from the Microsoft Store's public catalog (the
// product "Minecraft: Java & Bedrock Edition for PC"): the portrait poster as
// the cover, the wide hero art as the banner. Its "Logo" is a square tile,
// not a title logo, so the name stays text. Janus downloads them to
// ctx.gameDir/art (nonsteam.fetch_art); nothing is shipped with the handler.
const STORE_PRODUCT = "9NXP44L49SHJ";

Game.art = function (ctx) {
    const d = Janus.fetchJson("https://displaycatalog.mp.microsoft.com/v7.0/products" +
                              `?bigIds=${STORE_PRODUCT}&market=US&languages=en-us`);
    const images = d.Products[0].LocalizedProperties[0].Images;
    const url = (purpose) => {
        const i = images.find(x => x.ImagePurpose === purpose);
        return i ? "https:" + i.Uri : null;
    };
    return {cover: url("Poster"), hero: url("SuperHeroArt")};
};

// --- paths ---------------------------------------------------------------

const prismDir = (ctx) => ctx.gameDir + "/prism";
const prismBin = (ctx) => prismDir(ctx) + "/PrismLauncher";
const sharedDir = (ctx) => prismDir(ctx) + "/instances/" + INSTANCE;
const installFile = (ctx) => ctx.gameDir + "/install.json";

// --- accounts --------------------------------------------------------------

function msaAccounts(file) {
    try {
        const d = Janus.readJson(file);
        return (d.accounts || []).filter(a => a.type === "MSA" && a.profile && a.profile.name);
    } catch (e) {
        return [];
    }
}

// Microsoft's refresh token dies after 90 days without use; Prism refreshes
// it at every start and records when (msa.iat, seconds). An account last
// refreshed longer ago can't sign in any more: Prism asks for a new sign-in
// ("AADSTS70000: the grant is expired"). Unknown age = assume it's fine.
const TOKEN_DAYS = 85;

function daysSinceRefresh(a) {
    const iat = a.msa && a.msa.iat;
    return iat ? (Date.now() / 1000 - iat) / 86400 : 0;
}

const expired = (a) => daysSinceRefresh(a) > TOKEN_DAYS;

function ownerAccount(ctx) {
    const all = msaAccounts(prismDir(ctx) + "/accounts.json");
    return all.find(a => a.active) || all[0] || null;
}

// The user's own Prism's account: {file, name, expired, days}, or null.
function userPrismAccounts() {
    for (const f of USER_PRISM_ACCOUNTS) {
        const all = msaAccounts(f);
        if (!all.length) continue;
        const a = all.find(x => x.active && !expired(x)) || all.find(x => !expired(x)) ||
                  all.find(x => x.active) || all[0];
        return {file: f, name: a.profile.name, expired: expired(a),
                days: Math.round(daysSinceRefresh(a))};
    }
    return null;
}

// A valid Minecraft username: 3-16 of A-Z a-z 0-9 _.
function mcName(profile) {
    let n = String(profile.name).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 16);
    while (n.length < 3) n += "_";
    return n;
}

// --- versions (Modrinth + Fabric meta) ---------------------------------------

const q = (arr) => encodeURIComponent(JSON.stringify(arr));

function bestVersion(project, mc) {
    const vs = Janus.fetchJson(`${MODRINTH}/project/${project}/version` +
                               `?loaders=${q(["fabric"])}&game_versions=${q([mc])}`);
    return vs.find(v => v.version_type === "release") ||
           vs.find(v => v.version_type === "beta") || vs[0] || null;
}

// project -> {project_id: version} with its required dependencies, or null
// when something is missing for this Minecraft version.
function withDependencies(project, mc, chosen) {
    const out = Object.assign({}, chosen);
    const todo = [project];
    while (todo.length) {
        const p = todo.shift();
        const v = bestVersion(p, mc);
        if (!v) return null;
        if (out[v.project_id]) continue;
        out[v.project_id] = v;
        for (const d of v.dependencies || [])
            if (d.dependency_type === "required" && d.project_id && !out[d.project_id])
                todo.push(d.project_id);
    }
    return out;
}

// The newest stable Minecraft that Controlify (and its dependencies) support:
// {minecraft, loader, mods: [version objects]}.
function resolvePlan() {
    Janus.progress("Looking up the newest Minecraft that Controlify supports…");
    const supported = new Set();
    let controlifyId = null;
    for (const v of Janus.fetchJson(`${MODRINTH}/project/${REQUIRED_MODS[0]}/version?loaders=${q(["fabric"])}`)) {
        controlifyId = v.project_id;
        for (const g of v.game_versions) supported.add(g);
    }
    const games = Janus.fetchJson(`${FABRIC_META}/versions/game`)
        .filter(g => g.stable && supported.has(g.version));
    for (const g of games.slice(0, 5)) {
        const mc = g.version;
        let chosen = {};
        for (const p of REQUIRED_MODS) {
            chosen = chosen && withDependencies(p, mc, chosen);
        }
        if (!chosen) continue;
        for (const p of EXTRA_MODS) {
            const more = withDependencies(p, mc, chosen);
            if (more) chosen = more;
        }
        const loaders = Janus.fetchJson(`${FABRIC_META}/versions/loader/${mc}`);
        const loader = loaders.find(l => l.loader.stable) || loaders[0];
        if (!loader) continue;
        return {minecraft: mc, loader: loader.loader.version, mods: Object.values(chosen),
                controlifyId: controlifyId};
    }
    Janus.error("Couldn't find a Minecraft version that Controlify supports (Modrinth).");
}

function modLabel(v) {
    return v.name && v.name.length < 40 ? v.name : v.version_number;
}

function describe(inst) {
    const ctl = inst.mods.find(m => m.project === "controlify");
    return `Minecraft ${inst.minecraft}, Fabric ${inst.loader}` +
           (ctl ? `, Controlify ${ctl.version}` : "");
}

// --- install ---------------------------------------------------------------

function writeLauncherConfig(ctx) {
    // Without a language and theme Prism opens its first-run wizard.
    const lang = (Janus.run(["sh", "-c", "echo ${LANG%%.*}"]).stdout.trim() || "en_US");
    const cfg = prismDir(ctx) + "/prismlauncher.cfg";
    for (const [k, v] of [["ConfigVersion", "1.3"], ["ApplicationTheme", "system"],
                          ["IconTheme", "pe_colored"], ["Language", lang]])
        if (Janus.iniGet(cfg, "General", k) === null) Janus.iniSet(cfg, "General", k, v);
}

// Prism downloads a version's files (libraries, assets, Java) only when it
// launches it online, so launch the shared instance once with a wrapper that
// doesn't start the game. Prism exits when the "game" does.
function downloadGameFiles(ctx) {
    const owner = ownerAccount(ctx);
    if (!owner) Janus.error("Sign in to Prism Launcher first.");
    const warmup = ctx.gameDir + "/download-only.sh";
    Janus.writeFile(warmup, "#!/bin/sh\n# Janus: Prism only downloads the game files; nothing starts.\nexit 0\n");
    Janus.run(["chmod", "+x", warmup]);
    const cfg = sharedDir(ctx) + "/instance.cfg";
    Janus.iniSet(cfg, "General", "OverrideCommands", "true");
    Janus.iniSet(cfg, "General", "WrapperCommand", warmup);
    Janus.progress("Prism Launcher is downloading Minecraft…");
    const r = Janus.run([prismBin(ctx), "-d", prismDir(ctx), "-l", INSTANCE,
                         "-a", owner.profile.name], {timeout: 3600});
    if (r.code !== 0)
        Janus.error(`Prism Launcher couldn't download Minecraft (exit ${r.code}). ` +
                    "Open Prism Launcher from the game's options to see why.");
}

function install(ctx, plan) {
    const shared = sharedDir(ctx);
    const mods = shared + "/minecraft/mods";
    // Mods into a staging dir first: a failed download changes nothing.
    const staged = shared + "/minecraft/mods.new";
    if (Janus.exists(staged)) Janus.remove(staged);
    Janus.mkdir(staged);
    for (const v of plan.mods) {
        const f = v.files.find(x => x.primary) || v.files[0];
        Janus.download(f.url, staged + "/" + f.filename,
                       {sha512: f.hashes.sha512, label: f.filename});
    }
    // From here the old install is gone: until install.json is written
    // again, setupCheck offers "Install Minecraft" (never a half-updated one).
    if (Janus.exists(installFile(ctx))) Janus.remove(installFile(ctx));
    if (Janus.exists(mods)) Janus.remove(mods);
    Janus.move(staged, mods);
    Janus.writeJson(shared + "/mmc-pack.json", {
        formatVersion: 1,
        components: [
            {uid: "net.minecraft", version: plan.minecraft, important: true},
            {uid: "net.fabricmc.intermediary", version: plan.minecraft, dependencyOnly: true},
            {uid: "net.fabricmc.fabric-loader", version: plan.loader},
        ],
    });
    const cfg = shared + "/instance.cfg";
    for (const [k, v] of [["ConfigVersion", "1.3"], ["InstanceType", "OneSix"],
                          ["name", "Janus"]])
        Janus.iniSet(cfg, "General", k, v);
    downloadGameFiles(ctx);
    const inst = {
        minecraft: plan.minecraft, loader: plan.loader,
        mods: plan.mods.map(v => ({project: v.project_id === plan.controlifyId ? "controlify" : v.project_id,
                                   name: modLabel(v), version: v.version_number})),
    };
    Janus.writeJson(installFile(ctx), inst);
    return inst;
}

// --- setup steps and actions ----------------------------------------------

Game.setupCheck = function (ctx) {
    if (!Janus.exists(prismBin(ctx)))
        return {ready: false, action: "downloadPrism",
                message: "Minecraft is played through Prism Launcher. Janus downloads its " +
                         "own copy (about 60 MB, from github.com/PrismLauncher), separate " +
                         "from any Prism Launcher you have."};
    const signIn = "Sign in with the Microsoft account that owns Minecraft: Prism " +
                   "Launcher opens, add the account there (Accounts → Add Microsoft), then " +
                   "close Prism. Only this one account is needed: the other players play " +
                   "offline.";
    const owner = ownerAccount(ctx);
    if (!owner) {
        const theirs = userPrismAccounts();
        if (theirs && !theirs.expired)
            return {ready: false, action: "copyAccount",
                    message: `Your Prism Launcher is signed in as ${theirs.name}. Janus can ` +
                             "use that account. (To sign in with another one, choose Open " +
                             "Prism Launcher in the game's options.) Only this one account " +
                             "is needed: the other players play offline."};
        if (theirs)
            return {ready: false, action: "openPrism",
                    message: `Your Prism Launcher's sign-in as ${theirs.name} has expired ` +
                             `(last used ${theirs.days} days ago; Microsoft keeps it 90), ` +
                             "so Janus can't reuse it. " + signIn};
        return {ready: false, action: "openPrism", message: signIn};
    }
    if (expired(owner))
        return {ready: false, action: "openPrism",
                message: `The sign-in as ${owner.profile.name} has expired (Minecraft wasn't ` +
                         `played for ${Math.round(daysSinceRefresh(owner))} days). Prism ` +
                         "Launcher opens: remove the account and add it again, then close Prism."};
    if (!Janus.exists(installFile(ctx)))
        return {ready: false, action: "install",
                message: "Janus installs the newest Minecraft that Controlify (controller " +
                         "support) works with, with Fabric, Sodium and Mod Menu. Prism " +
                         "Launcher downloads the game (about 700 MB)."};
    return {ready: true, status: describe(Janus.readJson(installFile(ctx)))};
};

Game.owner = function (ctx) {
    const a = ownerAccount(ctx);
    return a ? {name: a.profile.name, label: "Microsoft account"} : null;
};

Janus.addAction("downloadPrism", {
    label: "Download Prism Launcher",
    menu: false,
    run: function (ctx) {
        const rel = Janus.fetchJson(PRISM_RELEASE);
        const arch = Janus.run(["uname", "-m"]).stdout.trim();
        const want = arch === "aarch64"
            ? /^PrismLauncher-Linux-aarch64-Qt6-Portable-.*\.tar\.gz$/
            : /^PrismLauncher-Linux-Qt6-Portable-.*\.tar\.gz$/;
        const asset = rel.assets.find(a => want.test(a.name));
        if (!asset) Janus.error(`Prism Launcher ${rel.tag_name} has no portable build for ${arch}.`);
        const opts = {label: "Prism Launcher " + rel.tag_name};
        if (asset.digest && asset.digest.startsWith("sha256:")) opts.sha256 = asset.digest.slice(7);
        const tgz = ctx.gameDir + "/prism-download.tar.gz";
        const tmp = ctx.gameDir + "/prism.new";
        Janus.download(asset.browser_download_url, tgz, opts);
        Janus.progress("Unpacking Prism Launcher…");
        if (Janus.exists(tmp)) Janus.remove(tmp);
        Janus.extract(tgz, tmp);
        Janus.remove(tgz);
        if (!Janus.exists(tmp + "/PrismLauncher")) Janus.error("Unexpected Prism Launcher download (no PrismLauncher inside).");
        Janus.move(tmp, prismDir(ctx));
        writeLauncherConfig(ctx);
        return `Prism Launcher ${rel.tag_name} downloaded.`;
    },
});

Janus.addAction("copyAccount", {
    label: "Use my Prism Launcher account",
    menu: false,
    run: function (ctx) {
        const theirs = userPrismAccounts();
        if (!theirs) Janus.error("Your Prism Launcher has no Microsoft account.");
        if (theirs.expired)
            Janus.error(`The sign-in as ${theirs.name} in your Prism Launcher has expired. ` +
                        "Sign in again in Janus's Prism Launcher instead.");
        Janus.copy(theirs.file, prismDir(ctx) + "/accounts.json");
        return `Signed in as ${theirs.name}.`;
    },
});

Janus.addAction("openPrism", {
    label: "Open Prism Launcher",
    description: "Sign in or switch accounts, look at Minecraft's logs, change " +
                 "Prism's settings. Close Prism Launcher to come back.",
    run: function (ctx) {
        if (!Janus.exists(prismBin(ctx))) Janus.error("Prism Launcher isn't downloaded yet.");
        Janus.progress("Prism Launcher is open. Close it to come back.");
        Janus.run([prismBin(ctx), "-d", prismDir(ctx)], {timeout: 86400});
        const a = ownerAccount(ctx);
        if (!a) return "No Microsoft account is signed in.";
        return expired(a) ? `The sign-in as ${a.profile.name} is still expired.`
                          : `Signed in as ${a.profile.name}.`;
    },
});

Janus.addAction("install", {
    label: "Install Minecraft",
    menu: false,
    run: function (ctx) {
        return "Installed " + describe(install(ctx, resolvePlan())) + ".";
    },
});

Janus.addAction("update", {
    label: "Update Minecraft",
    description: "Install the newest Minecraft that Controlify works with. A world " +
                 "opened in a newer Minecraft can't be played in an older one again.",
    run: function (ctx) {
        if (!Janus.exists(installFile(ctx))) Janus.error("Minecraft isn't installed yet.");
        const cur = Janus.readJson(installFile(ctx));
        const plan = resolvePlan();
        const same = plan.minecraft === cur.minecraft && plan.loader === cur.loader &&
            plan.mods.length === cur.mods.length &&
            plan.mods.every(v => cur.mods.some(m => m.version === v.version_number));
        if (same) return `Up to date: ${describe(cur)}.`;
        return `Updated from ${describe(cur)} to ${describe(install(ctx, plan))}.`;
    },
});

// --- launch ----------------------------------------------------------------

function memoryMb(ctx) {
    const opt = ctx.options.memory;
    if (opt && opt !== "auto") return Math.round(parseFloat(opt) * 1024);
    const total = ctx.memTotalMb || 8192;
    const n = Math.max(1, ctx.players.length);
    const per = (total - Math.max(4096, total * 0.25)) / n;
    return Math.min(6144, Math.max(2048, Math.floor(per / 512) * 512));
}

// Minecraft's options.txt is key:value lines; set *values* where the player
// hasn't got the key yet (their own choices stay).
function defaultOptions(file, values) {
    const lines = Janus.exists(file) ? Janus.readLines(file) : [];
    const have = new Set(lines.map(l => l.split(":")[0]));
    const add = Object.entries(values).filter(([k]) => !have.has(k)).map(([k, v]) => `${k}:${v}`);
    if (add.length) Janus.writeLines(file, lines.concat(add));
}

// Controlify's on-screen keyboard types nothing on Minecraft 26: opening it
// clears the focus of the text field underneath, and its Del/arrow keys come
// without the SDL keycode EditBox needs. payload/janus-controlify-fix.jar (a
// tiny Fabric mixin mod, source + build.sh in payload/controlify-fix/) fixes
// both. See NOTES.md "Minecraft: Controlify's keyboard typed nothing".
const FIX_MOD = "janus-controlify-fix.jar";

function controlifyFix(ctx) {
    const mods = sharedDir(ctx) + "/minecraft/mods/" + FIX_MOD;
    const inst = Janus.exists(installFile(ctx)) ? Janus.readJson(installFile(ctx)) : null;
    if (inst && /^26\./.test(inst.minecraft)) {
        Janus.mkdir(ctx.gameDir + "/payload");
        Janus.deployPayload(ctx.gameDir + "/payload");
        Janus.copy(ctx.gameDir + "/payload/" + FIX_MOD, mods);
    } else if (Janus.exists(mods)) {
        Janus.remove(mods);
    }
}

Game.buildCommand = function (ctx) {
    // Two profiles must not end up with the same Minecraft name.
    const seen = {};
    for (const p of ctx.players) {
        const n = mcName(p.profile).toLowerCase();
        if (seen[n] !== undefined)
            Janus.error(`"${seen[n]}" and "${p.profile.name}" would both play as ` +
                        `${mcName(p.profile)} in Minecraft. Rename one of them.`);
        seen[n] = p.profile.name;
    }
    const total = ctx.memTotalMb, mem = memoryMb(ctx);
    if (total && mem * ctx.players.length > total - 2048)
        Janus.warn(`${ctx.players.length} players × ${mem / 1024} GB is more memory than ` +
                   `this computer can spare (${Math.round(total / 1024)} GB). Lower ` +
                   `"Memory per player" in the game's options if Minecraft is slow or crashes.`);

    // This player's Prism: own settings and worlds, shared everything else.
    controlifyFix(ctx);
    const shared = sharedDir(ctx), main = prismDir(ctx);
    const dir = ctx.saveRoot + "/prism";
    const inst = dir + "/instances/" + INSTANCE;
    Janus.mkdir(inst + "/minecraft");
    for (const d of ["libraries", "assets", "java", "meta"]) {
        Janus.mkdir(main + "/" + d);
        Janus.symlink(main + "/" + d, dir + "/" + d);
    }
    Janus.symlink(main + "/accounts.json", dir + "/accounts.json");
    Janus.copy(main + "/prismlauncher.cfg", dir + "/prismlauncher.cfg");
    Janus.copy(shared + "/mmc-pack.json", inst + "/mmc-pack.json");
    Janus.copy(shared + "/instance.cfg", inst + "/instance.cfg");
    if (!Janus.exists(inst + "/minecraft/mods")) Janus.symlink(shared + "/minecraft/mods", inst + "/minecraft/mods");
    const cfg = inst + "/instance.cfg";
    for (const [k, v] of [
        ["OverrideCommands", "true"], ["WrapperCommand", ctx.wrapper],
        ["OverrideMemory", "true"], ["MinMemAlloc", "512"], ["MaxMemAlloc", String(mem)],
        ["OverrideWindow", "true"], ["LaunchMaximized", "false"],
        ["MinecraftWinWidth", String(ctx.width || 1280)],
        ["MinecraftWinHeight", String(ctx.height || 720)],
        // No console window: Prism shows it when the game exits "with an
        // error" (gamescope's exit code counts) and then doesn't exit itself,
        // so the session would never end. Logs: <instance>/minecraft/logs.
        ["OverrideConsole", "true"], ["ShowConsole", "false"],
        ["ShowConsoleOnError", "false"], ["AutoCloseConsole", "true"],
    ])
        Janus.iniSet(cfg, "General", k, v);
    defaultOptions(inst + "/minecraft/options.txt", {
        onboardAccessibility: "false",     // the narrator question at first start
        skipMultiplayerWarning: "true",    // "Third-Party Online Play"
        joinedFirstServer: "true",
        pauseOnLostFocus: "false",         // only one tile has the focus
    });

    const owner = ownerAccount(ctx);
    const who = ctx.profile.owner && owner
        ? ["-a", owner.profile.name]         // online, with the account's skin
        : ["-o", mcName(ctx.profile)];
    return [prismBin(ctx), "-d", dir, "-l", INSTANCE].concat(who);
};
