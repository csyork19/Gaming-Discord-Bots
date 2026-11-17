"""
League stats bot for Discord: season stats and league leaders for a
football league (passing / rushing) and a 2K league (points per game).

Stats are stored in a local SQLite file (league_stats.db) and entered by
admins with slash commands or a CSV upload. Each Discord server keeps its
own separate stats.

-----------------------------------------------------------------------
SETUP
  1. pip install -U discord.py
  2. Create a bot at https://discord.com/developers/applications
     and invite it with the "bot" and "applications.commands" scopes.
  3. Set environment variables and run:
        DISCORD_TOKEN=your-bot-token
        GUILD_ID=your-server-id      (optional: makes commands show up instantly)
        python league_stats_bot.py

-----------------------------------------------------------------------
COMMANDS (everyone)
  /leaders passing  [season] [top]      Passing yards leaders
  /leaders rushing  [season] [top]      Rushing yards leaders
  /leaders ppg      [season] [top] [min_games]   2K points-per-game leaders
  /stats football   player [season]     A player's football season stats
  /stats 2k         player [season]     A player's 2K season stats

COMMANDS (admins with "Manage Server")
  /addstats football  player team week ...   Add/overwrite one game's stats
  /addstats 2k        player team game ...   Add/overwrite one game's stats
  /league season      number                 Set the current season
  /league import      game file              Bulk import from a CSV file
  /league delete      game player number     Remove one game entry

  Re-entering the same player + week (or game) overwrites that entry,
  so you can fix mistakes by simply entering it again.

-----------------------------------------------------------------------
CSV IMPORT COLUMNS (header row required; missing stat columns count as 0;
season is optional and defaults to the current season)

  Football: season,week,player,team,pass_cmp,pass_att,pass_yds,pass_td,pass_int,rush_att,rush_yds,rush_td
  2K:       season,game,player,team,pts,reb,ast,stl,blk
"""

import csv
import io
import logging
import os
import sqlite3
from typing import Optional

import discord
from discord import app_commands

DB_PATH = os.environ.get("STATS_DB", "league_stats.db")

FOOTBALL_FIELDS = ["pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
                   "rush_att", "rush_yds", "rush_td"]
HOOPS_FIELDS = ["pts", "reb", "ast", "stl", "blk"]

GAMES = {
    "football": {"table": "football_games", "num": "week", "fields": FOOTBALL_FIELDS},
    "2k": {"table": "hoops_games", "num": "game", "fields": HOOPS_FIELDS},
}

# Friendly CSV header aliases -> real column names
CSV_ALIASES = {
    "int": "pass_int", "ints": "pass_int", "interceptions": "pass_int",
    "points": "pts", "rebounds": "reb", "assists": "ast", "steals": "stl", "blocks": "blk",
    "name": "player", "player_name": "player",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    season   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS football_games (
    guild_id INTEGER NOT NULL,
    season   INTEGER NOT NULL,
    week     INTEGER NOT NULL,
    player   TEXT NOT NULL COLLATE NOCASE,
    team     TEXT NOT NULL DEFAULT '',
    pass_cmp INTEGER NOT NULL DEFAULT 0,
    pass_att INTEGER NOT NULL DEFAULT 0,
    pass_yds INTEGER NOT NULL DEFAULT 0,
    pass_td  INTEGER NOT NULL DEFAULT 0,
    pass_int INTEGER NOT NULL DEFAULT 0,
    rush_att INTEGER NOT NULL DEFAULT 0,
    rush_yds INTEGER NOT NULL DEFAULT 0,
    rush_td  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, season, week, player)
);
CREATE TABLE IF NOT EXISTS hoops_games (
    guild_id INTEGER NOT NULL,
    season   INTEGER NOT NULL,
    game     INTEGER NOT NULL,
    player   TEXT NOT NULL COLLATE NOCASE,
    team     TEXT NOT NULL DEFAULT '',
    pts      INTEGER NOT NULL DEFAULT 0,
    reb      INTEGER NOT NULL DEFAULT 0,
    ast      INTEGER NOT NULL DEFAULT 0,
    stl      INTEGER NOT NULL DEFAULT 0,
    blk      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, season, game, player)
);
"""


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

class StatsDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- season ---
    def get_season(self, guild_id: int) -> int:
        row = self.conn.execute("SELECT season FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
        return row["season"] if row else 1

    def set_season(self, guild_id: int, season: int):
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (guild_id, season) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET season=excluded.season",
                (guild_id, season))

    # --- writes ---
    def upsert(self, game: str, guild_id: int, season: int, num: int,
               player: str, team: str, stats: dict, commit: bool = True):
        g = GAMES[game]
        cols = ["guild_id", "season", g["num"], "player", "team", *g["fields"]]
        vals = [guild_id, season, num, player.strip(), (team or "").strip(),
                *(int(stats.get(f) or 0) for f in g["fields"])]
        updates = ", ".join(f"{c}=excluded.{c}" for c in ["team", *g["fields"]])
        sql = (f"INSERT INTO {g['table']} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))}) "
               f"ON CONFLICT(guild_id, season, {g['num']}, player) DO UPDATE SET {updates}")
        self.conn.execute(sql, vals)
        if commit:
            self.conn.commit()

    def delete(self, game: str, guild_id: int, season: int, num: int, player: str) -> int:
        g = GAMES[game]
        with self.conn:
            cur = self.conn.execute(
                f"DELETE FROM {g['table']} WHERE guild_id=? AND season=? AND {g['num']}=? AND player=?",
                (guild_id, season, num, player.strip()))
        return cur.rowcount

    def import_csv(self, game: str, guild_id: int, text: str, default_season: int):
        """Returns (rows_imported, list_of_error_strings)."""
        g = GAMES[game]
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return 0, ["The file is empty or has no header row."]
        imported, errors = 0, []
        for line_no, raw in enumerate(reader, start=2):
            row = {}
            for k, v in raw.items():
                if k is None:
                    continue
                key = k.strip().lower().replace(" ", "_")
                row[CSV_ALIASES.get(key, key)] = (v or "").strip()
            try:
                player = row.get("player", "")
                if not player:
                    raise ValueError("missing player")
                num = int(row.get(g["num"], ""))
                season = int(row["season"]) if row.get("season") else default_season
                stats = {f: int(float(row[f])) if row.get(f) else 0 for f in g["fields"]}
                self.upsert(game, guild_id, season, num, player, row.get("team", ""), stats, commit=False)
                imported += 1
            except (ValueError, KeyError) as e:
                errors.append(f"Line {line_no}: {e}")
        self.conn.commit()
        return imported, errors

    # --- reads ---
    def _team_expr(self, game: str) -> str:
        g = GAMES[game]
        return (f"(SELECT t.team FROM {g['table']} t WHERE t.guild_id=x.guild_id AND t.season=x.season "
                f"AND t.player=x.player ORDER BY t.{g['num']} DESC LIMIT 1)")

    def passing_leaders(self, guild_id: int, season: int, top: int):
        return self.conn.execute(f"""
            SELECT player, {self._team_expr('football')} AS team, COUNT(*) AS gp,
                   SUM(pass_cmp) AS cmp, SUM(pass_att) AS att, SUM(pass_yds) AS yds,
                   SUM(pass_td) AS td, SUM(pass_int) AS ints
            FROM football_games x WHERE guild_id=? AND season=?
            GROUP BY player HAVING SUM(pass_att) > 0
            ORDER BY yds DESC, td DESC LIMIT ?""", (guild_id, season, top)).fetchall()

    def rushing_leaders(self, guild_id: int, season: int, top: int):
        return self.conn.execute(f"""
            SELECT player, {self._team_expr('football')} AS team, COUNT(*) AS gp,
                   SUM(rush_att) AS att, SUM(rush_yds) AS yds, SUM(rush_td) AS td
            FROM football_games x WHERE guild_id=? AND season=?
            GROUP BY player HAVING SUM(rush_att) > 0
            ORDER BY yds DESC, td DESC LIMIT ?""", (guild_id, season, top)).fetchall()

    def ppg_leaders(self, guild_id: int, season: int, top: int, min_games: int):
        return self.conn.execute(f"""
            SELECT player, {self._team_expr('2k')} AS team, COUNT(*) AS gp,
                   AVG(pts) AS ppg, AVG(reb) AS rpg, AVG(ast) AS apg
            FROM hoops_games x WHERE guild_id=? AND season=?
            GROUP BY player HAVING COUNT(*) >= ?
            ORDER BY ppg DESC LIMIT ?""", (guild_id, season, min_games, top)).fetchall()

    def football_player(self, guild_id: int, season: int, player: str):
        row = self.conn.execute(f"""
            SELECT player, {self._team_expr('football')} AS team, COUNT(*) AS gp,
                   SUM(pass_cmp) AS cmp, SUM(pass_att) AS att, SUM(pass_yds) AS pyds,
                   SUM(pass_td) AS ptd, SUM(pass_int) AS ints,
                   SUM(rush_att) AS ratt, SUM(rush_yds) AS ryds, SUM(rush_td) AS rtd
            FROM football_games x WHERE guild_id=? AND season=? AND player=?
            GROUP BY player""", (guild_id, season, player.strip())).fetchone()
        return row

    def hoops_player(self, guild_id: int, season: int, player: str):
        return self.conn.execute(f"""
            SELECT player, {self._team_expr('2k')} AS team, COUNT(*) AS gp,
                   SUM(pts) AS pts, SUM(reb) AS reb, SUM(ast) AS ast, SUM(stl) AS stl, SUM(blk) AS blk,
                   MAX(pts) AS high
            FROM hoops_games x WHERE guild_id=? AND season=? AND player=?
            GROUP BY player""", (guild_id, season, player.strip())).fetchone()

    def player_names(self, game: str, guild_id: int, starts_with: str, limit: int = 25):
        g = GAMES[game]
        rows = self.conn.execute(
            f"SELECT DISTINCT player FROM {g['table']} WHERE guild_id=? AND player LIKE ? "
            f"ORDER BY player LIMIT ?", (guild_id, f"%{starts_with}%", limit)).fetchall()
        return [r["player"] for r in rows]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def passer_rating(cmp, att, yds, td, ints) -> float:
    if not att:
        return 0.0
    clamp = lambda v: max(0.0, min(2.375, v))
    a = clamp((cmp / att - 0.3) * 5)
    b = clamp((yds / att - 3) * 0.25)
    c = clamp(td / att * 20)
    d = clamp(2.375 - ints / att * 25)
    return (a + b + c + d) / 6 * 100


def text_table(headers, rows, name_col=1, name_width=14) -> str:
    """Monospace table inside a code block. Column `name_col` is left-aligned and truncated."""
    def cell(i, v):
        s = str(v)
        if i == name_col and len(s) > name_width:
            s = s[: name_width - 1] + "…"
        return s
    data = [[cell(i, v) for i, v in enumerate(r)] for r in rows]
    widths = [max(len(str(h)), *(len(r[i]) for r in data)) for i, h in enumerate(headers)]

    def line(vals):
        out = []
        for i, v in enumerate(vals):
            out.append(str(v).ljust(widths[i]) if i <= name_col else str(v).rjust(widths[i]))
        return "  ".join(out).rstrip()

    return "```\n" + "\n".join([line(headers), *(line(r) for r in data)]) + "\n```"


FOOTBALL_COLOR = discord.Color.from_rgb(1, 51, 105)
HOOPS_COLOR = discord.Color.from_rgb(201, 8, 42)


# ---------------------------------------------------------------------------
# Bot + commands
# ---------------------------------------------------------------------------

db = StatsDB(DB_PATH)
log = logging.getLogger("league-stats-bot")


class LeagueBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        for group in (leaders, stats, addstats, league):
            self.tree.add_command(group)
        guild_id = os.environ.get("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to server %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Commands synced globally (may take a while to appear)")

    async def on_ready(self):
        log.info("Logged in as %s", self.user)


def season_or_current(interaction: discord.Interaction, season: Optional[int]) -> int:
    return season if season is not None else db.get_season(interaction.guild_id)


async def football_player_ac(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n)
            for n in db.player_names("football", interaction.guild_id, current)]


async def hoops_player_ac(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n)
            for n in db.player_names("2k", interaction.guild_id, current)]


# ---- /leaders ----
leaders = app_commands.Group(name="leaders", description="League leaders", guild_only=True)


@leaders.command(name="passing", description="Passing yards leaders")
@app_commands.describe(season="Season number (defaults to current)", top="How many players to show")
async def leaders_passing(interaction: discord.Interaction, season: Optional[int] = None,
                          top: app_commands.Range[int, 1, 25] = 10):
    s = season_or_current(interaction, season)
    rows = db.passing_leaders(interaction.guild_id, s, top)
    if not rows:
        return await interaction.response.send_message(f"No passing stats for season {s} yet.")
    table = text_table(
        ["#", "Player", "Yds", "TD", "INT", "Cmp%"],
        [[i, r["player"], r["yds"], r["td"], r["ints"], f"{r['cmp'] / r['att'] * 100:.1f}"]
         for i, r in enumerate(rows, 1)])
    embed = discord.Embed(title=f"🏈 Passing Leaders — Season {s}", description=table, color=FOOTBALL_COLOR)
    await interaction.response.send_message(embed=embed)


@leaders.command(name="rushing", description="Rushing yards leaders")
@app_commands.describe(season="Season number (defaults to current)", top="How many players to show")
async def leaders_rushing(interaction: discord.Interaction, season: Optional[int] = None,
                          top: app_commands.Range[int, 1, 25] = 10):
    s = season_or_current(interaction, season)
    rows = db.rushing_leaders(interaction.guild_id, s, top)
    if not rows:
        return await interaction.response.send_message(f"No rushing stats for season {s} yet.")
    table = text_table(
        ["#", "Player", "Yds", "Att", "YPC", "TD"],
        [[i, r["player"], r["yds"], r["att"], f"{r['yds'] / r['att']:.1f}", r["td"]]
         for i, r in enumerate(rows, 1)])
    embed = discord.Embed(title=f"🏈 Rushing Leaders — Season {s}", description=table, color=FOOTBALL_COLOR)
    await interaction.response.send_message(embed=embed)


@leaders.command(name="ppg", description="2K points-per-game leaders")
@app_commands.describe(season="Season number (defaults to current)", top="How many players to show",
                       min_games="Minimum games played to qualify")
async def leaders_ppg(interaction: discord.Interaction, season: Optional[int] = None,
                      top: app_commands.Range[int, 1, 25] = 10,
                      min_games: app_commands.Range[int, 1, 100] = 1):
    s = season_or_current(interaction, season)
    rows = db.ppg_leaders(interaction.guild_id, s, top, min_games)
    if not rows:
        return await interaction.response.send_message(f"No 2K stats for season {s} yet.")
    table = text_table(
        ["#", "Player", "GP", "PPG", "RPG", "APG"],
        [[i, r["player"], r["gp"], f"{r['ppg']:.1f}", f"{r['rpg']:.1f}", f"{r['apg']:.1f}"]
         for i, r in enumerate(rows, 1)])
    embed = discord.Embed(title=f"🏀 Points Per Game — Season {s}", description=table, color=HOOPS_COLOR)
    if min_games > 1:
        embed.set_footer(text=f"Minimum {min_games} games played")
    await interaction.response.send_message(embed=embed)


# ---- /stats ----
stats = app_commands.Group(name="stats", description="Player season stats", guild_only=True)


@stats.command(name="football", description="A player's football season stats")
@app_commands.describe(player="Player name", season="Season number (defaults to current)")
@app_commands.autocomplete(player=football_player_ac)
async def stats_football(interaction: discord.Interaction, player: str, season: Optional[int] = None):
    s = season_or_current(interaction, season)
    r = db.football_player(interaction.guild_id, s, player)
    if not r:
        return await interaction.response.send_message(
            f"No football stats for **{player}** in season {s}.", ephemeral=True)

    title = f"🏈 {r['player']}" + (f" ({r['team']})" if r["team"] else "") + f" — Season {s}"
    embed = discord.Embed(title=title, color=FOOTBALL_COLOR)
    embed.add_field(name="Games", value=str(r["gp"]), inline=False)
    if r["att"]:
        embed.add_field(name="Passing", value=(
            f"{r['cmp']}/{r['att']} ({r['cmp'] / r['att'] * 100:.1f}%)\n"
            f"{r['pyds']:,} yds · {r['pyds'] / r['gp']:.1f}/game\n"
            f"{r['ptd']} TD · {r['ints']} INT\n"
            f"Rating {passer_rating(r['cmp'], r['att'], r['pyds'], r['ptd'], r['ints']):.1f}"))
    if r["ratt"]:
        embed.add_field(name="Rushing", value=(
            f"{r['ratt']} att · {r['ryds']:,} yds\n"
            f"{r['ryds'] / r['ratt']:.1f} YPC · {r['ryds'] / r['gp']:.1f}/game\n"
            f"{r['rtd']} TD"))
    await interaction.response.send_message(embed=embed)


@stats.command(name="2k", description="A player's 2K season stats")
@app_commands.describe(player="Player name", season="Season number (defaults to current)")
@app_commands.autocomplete(player=hoops_player_ac)
async def stats_2k(interaction: discord.Interaction, player: str, season: Optional[int] = None):
    s = season_or_current(interaction, season)
    r = db.hoops_player(interaction.guild_id, s, player)
    if not r:
        return await interaction.response.send_message(
            f"No 2K stats for **{player}** in season {s}.", ephemeral=True)

    gp = r["gp"]
    title = f"🏀 {r['player']}" + (f" ({r['team']})" if r["team"] else "") + f" — Season {s}"
    embed = discord.Embed(title=title, color=HOOPS_COLOR)
    embed.add_field(name="Averages", value=(
        f"**{r['pts'] / gp:.1f}** PPG · {r['reb'] / gp:.1f} RPG · {r['ast'] / gp:.1f} APG\n"
        f"{r['stl'] / gp:.1f} SPG · {r['blk'] / gp:.1f} BPG"), inline=False)
    embed.add_field(name="Totals", value=(
        f"{gp} games · {r['pts']:,} pts · {r['reb']:,} reb · {r['ast']:,} ast\n"
        f"Season high: {r['high']} pts"), inline=False)
    await interaction.response.send_message(embed=embed)


# ---- /addstats (admin) ----
admin_perms = discord.Permissions(manage_guild=True)
addstats = app_commands.Group(name="addstats", description="Enter game stats (admins)",
                              guild_only=True, default_permissions=admin_perms)


@addstats.command(name="football", description="Add or overwrite a player's stats for one week")
@app_commands.describe(week="Week number", season="Season (defaults to current)",
                       pass_cmp="Completions", pass_att="Pass attempts", pass_yds="Passing yards",
                       pass_td="Passing TDs", pass_int="Interceptions thrown",
                       rush_att="Rush attempts", rush_yds="Rushing yards", rush_td="Rushing TDs")
@app_commands.autocomplete(player=football_player_ac)
async def addstats_football(interaction: discord.Interaction, player: str, team: str, week: int,
                            pass_cmp: int = 0, pass_att: int = 0, pass_yds: int = 0,
                            pass_td: int = 0, pass_int: int = 0,
                            rush_att: int = 0, rush_yds: int = 0, rush_td: int = 0,
                            season: Optional[int] = None):
    if pass_cmp > pass_att:
        return await interaction.response.send_message(
            "Completions can't be more than attempts.", ephemeral=True)
    s = season_or_current(interaction, season)
    db.upsert("football", interaction.guild_id, s, week, player, team, {
        "pass_cmp": pass_cmp, "pass_att": pass_att, "pass_yds": pass_yds,
        "pass_td": pass_td, "pass_int": pass_int,
        "rush_att": rush_att, "rush_yds": rush_yds, "rush_td": rush_td})
    await interaction.response.send_message(
        f"✅ Saved **{player}** ({team}) — Season {s}, Week {week}: "
        f"{pass_cmp}/{pass_att}, {pass_yds} pass yds, {pass_td} TD, {pass_int} INT · "
        f"{rush_att} rush, {rush_yds} yds, {rush_td} TD", ephemeral=True)


@addstats.command(name="2k", description="Add or overwrite a player's stats for one game")
@app_commands.describe(game="Game number", season="Season (defaults to current)",
                       pts="Points", reb="Rebounds", ast="Assists", stl="Steals", blk="Blocks")
@app_commands.autocomplete(player=hoops_player_ac)
async def addstats_2k(interaction: discord.Interaction, player: str, team: str, game: int,
                      pts: int, reb: int = 0, ast: int = 0, stl: int = 0, blk: int = 0,
                      season: Optional[int] = None):
    s = season_or_current(interaction, season)
    db.upsert("2k", interaction.guild_id, s, game, player, team,
              {"pts": pts, "reb": reb, "ast": ast, "stl": stl, "blk": blk})
    await interaction.response.send_message(
        f"✅ Saved **{player}** ({team}) — Season {s}, Game {game}: "
        f"{pts} pts, {reb} reb, {ast} ast, {stl} stl, {blk} blk", ephemeral=True)


# ---- /league (admin) ----
league = app_commands.Group(name="league", description="League settings (admins)",
                            guild_only=True, default_permissions=admin_perms)
GAME_CHOICES = [app_commands.Choice(name="Football", value="football"),
                app_commands.Choice(name="2K", value="2k")]


@league.command(name="season", description="Set the current season")
@app_commands.describe(number="Season number")
async def league_season(interaction: discord.Interaction, number: app_commands.Range[int, 1, 1000]):
    db.set_season(interaction.guild_id, number)
    await interaction.response.send_message(f"📅 Current season is now **{number}**.")


@league.command(name="import", description="Bulk import stats from a CSV file")
@app_commands.describe(game="Which league the file is for", file="CSV file with a header row")
@app_commands.choices(game=GAME_CHOICES)
async def league_import(interaction: discord.Interaction, game: app_commands.Choice[str],
                        file: discord.Attachment):
    if not file.filename.lower().endswith(".csv"):
        return await interaction.response.send_message("Please upload a .csv file.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    count, errors = db.import_csv(game.value, interaction.guild_id, text,
                                  db.get_season(interaction.guild_id))
    msg = f"✅ Imported {count} {game.name} row(s)."
    if errors:
        shown = "\n".join(errors[:10])
        more = f"\n…and {len(errors) - 10} more" if len(errors) > 10 else ""
        msg += f"\n⚠️ Skipped {len(errors)} row(s):\n```\n{shown}{more}\n```"
    await interaction.followup.send(msg, ephemeral=True)


@league.command(name="delete", description="Remove one game entry for a player")
@app_commands.describe(game="Which league", player="Player name",
                       number="Week (football) or game number (2K)", season="Season (defaults to current)")
@app_commands.choices(game=GAME_CHOICES)
async def league_delete(interaction: discord.Interaction, game: app_commands.Choice[str], player: str,
                        number: int, season: Optional[int] = None):
    s = season_or_current(interaction, season)
    removed = db.delete(game.value, interaction.guild_id, s, number, player)
    label = "Week" if game.value == "football" else "Game"
    if removed:
        await interaction.response.send_message(
            f"🗑️ Removed {game.name} stats for **{player}**, Season {s} {label} {number}.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"No {game.name} entry found for **{player}**, Season {s} {label} {number}.", ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LeagueBot().run(os.environ["DISCORD_TOKEN"], log_handler=None)
