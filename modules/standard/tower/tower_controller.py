import time
from datetime import datetime

import pytz
import requests

from core.chat_blob import ChatBlob
from core.command_param_types import Any, Int, Const, Options, Time
from core.db import DB
from core.decorators import instance, command
from core.dict_object import DictObject
from core.feature_flags import FeatureFlags
from core.logger import Logger
from core.setting_types import TextSettingType, DictionarySettingType
from core.text import Text
from core.tyrbot import Tyrbot
from modules.standard.helpbot.playfield_controller import PlayfieldController


@instance()
class TowerController:
    def __init__(self):
        self.logger = Logger(__name__)

    def inject(self, registry):
        self.bot: Tyrbot = registry.get_instance("bot")
        self.db: DB = registry.get_instance("db")
        self.text: Text = registry.get_instance("text")
        self.util = registry.get_instance("util")
        self.command_alias_service = registry.get_instance("command_alias_service")
        self.setting_service = registry.get_instance("setting_service")
        self.playfield_controller: PlayfieldController = registry.get_instance("playfield_controller")
        self.level_controller = registry.get_instance("level_controller")

    def pre_start(self):
        self.db.load_sql_file(self.module_dir + "/" + "tower_site.sql")
        self.db.load_sql_file(self.module_dir + "/" + "tower_site_bounds.sql")

    def start(self):
        self.command_alias_service.add_alias("hot", "lc open")

        self.setting_service.register(self.module_name, "tower_api_address", "https://tower-api.jkbff.com/v1/api/towers",
                                      TextSettingType(["https://tower-api.jkbff.com/v1/api/towers"]),
                                      "The address of the Tower API")
        self.setting_service.register(self.module_name, "tower_api_custom_headers", "",
                                      DictionarySettingType(),
                                      "Custom headers for the Tower API")

    @command(command="lc", params=[], access_level="guest",
             description="See a list of playfields containing land control tower sites")
    def lc_list_cmd(self, request):
        data = self.db.query("SELECT id, long_name, short_name FROM playfields WHERE id IN (SELECT DISTINCT playfield_id FROM tower_site) ORDER BY short_name")

        blob = ""
        for row in data:
            blob += "[%d] %s <highlight>%s</highlight>\n" % (row.id, self.text.make_tellcmd(row.long_name, "lc %s" % row.short_name), row.short_name)

        blob += "\n" + self.get_lc_blob_footer()

        return ChatBlob("Land Control Playfields (%d)" % len(data), blob)

    if FeatureFlags.USE_TOWER_API:
        @command(command="lc", params=[Const("org"), Any("org", is_optional=True)], access_level="guest",
                 description="See a list of land control tower sites by org")
        def lc_org_cmd(self, request, _, org):
            params = list()
            params.append(("enabled", "true"))
            if not org:
                org = str(request.conn.org_id)
                if not org:
                    return "Bot does not belong to an org so an org name or org id must be specified."

            if org.isdigit():
                params.append(("org_id", org))
            else:
                for org_name_piece in org.split(" "):
                    params.append(("org_name", "%" + org_name_piece + "%"))

            data = self.lookup_tower_info(params).results

            t = int(time.time())
            grouped_data = self.util.group_by(data, lambda x: (x.org_id, x.org_name))
            blob = ""
            for k, v in grouped_data.items():
                v = sorted(v, key=lambda x: x.ql)

                org_blob = ""
                ct_types = []
                ql_total = 0
                for ct in v:
                    ct_types.append(self.get_ct_type(ct.ql))
                    ql_total += ct.ql
                    org_blob += self.format_site_info(ct, t) + "\n"

                blob += f"<pagebreak><header2>{k[1]} ({k[0]})</header2>"
                blob += " Types: <highlight>" + ", ".join(ct_types) + f"</highlight> Total CT QL: <highlight>{ql_total}</highlight>\n\n"
                blob += org_blob + "\n"

            return ChatBlob(f"Org Info for '{org}' ({len(data)})", blob)

        @command(command="lc", params=[Options(["all", "open", "closed", "penalty", "unplanted", "disabled"]),
                                       Options(["omni", "clan", "neutral", "all"], is_optional=True),
                                       Int("pvp_level", is_optional=True),
                                       Time("time", is_optional=True)],
                 access_level="guest", description="See a list of land control tower sites by QL, faction, and open status",
                 extended_description="The time param only applies when the first param is either 'open' or 'closed'")
        def lc_search_cmd(self, request, site_status, faction, pvp_level, time_offset):
            t = int(time.time())
            relative_time = t + (time_offset or 0)

            min_ql = 1
            max_ql = 300
            if pvp_level:
                level_info = self.level_controller.get_level_info(pvp_level)
                if not level_info:
                    return "PVP level must be between 1 and 220."
                min_ql = level_info.pvp_min
                max_ql = level_info.pvp_max

            params = list()

            if site_status.lower() == "disabled":
                params.append(("enabled", "false"))
            else:
                params.append(("enabled", "true"))

            if min_ql > 1:
                params.append(("min_ql", min_ql))

            if max_ql < 300:
                params.append(("max_ql", max_ql))

            if faction and faction != "all":
                params.append(("faction", faction))

            if site_status.lower() == "open":
                params.append(("min_close_time", relative_time))
                params.append(("max_close_time", relative_time + (3600 * 6)))
            elif site_status.lower() == "closed":
                params.append(("min_close_time", relative_time + (3600 * 6)))
                params.append(("max_close_time", relative_time + (3600 * 24)))
            elif site_status.lower() == "penalty":
                params.append(("penalty", "true"))
            elif site_status.lower() == "unplanted":
                params.append(("planted", "false"))

            data = self.lookup_tower_info(params).results

            blob = ""
            for row in data:
                blob += "<pagebreak>" + self.format_site_info(row, t) + "\n"

            if blob:
                blob += self.get_lc_blob_footer()

            title = "Tower Info: %s" % site_status.capitalize()
            if min_ql > 1 or max_ql < 300:
                title += " QL %d - %d" % (min_ql, max_ql)
            if faction:
                title += " [%s]" % faction.capitalize()
            if time_offset:
                title += " in " + self.util.time_to_readable(time_offset)
            title += " (%d)" % len(data)

            return ChatBlob(title, blob)

    @command(command="lc", params=[Any("playfield"), Int("site_number", is_optional=True)], access_level="guest",
             description="See a list of land control tower sites in a particular playfield")
    def lc_playfield_cmd(self, request, playfield_name, site_number):
        playfield = self.playfield_controller.get_playfield_by_name_or_id(playfield_name)
        if not playfield:
            return f"Could not find playfield <highlight>{playfield_name}</highlight>."

        data = self.get_tower_site_info(playfield.id, site_number)

        blob = ""
        t = int(time.time())
        for row in data:
            blob += "<pagebreak>" + self.format_site_info(row, t) + "\n"

        blob += self.get_lc_blob_footer()

        if site_number:
            title = "Tower Info: %s %d" % (playfield.long_name, site_number)
        else:
            title = "Tower Info: %s (%d)" % (playfield.long_name, len(data))

        return ChatBlob(title, blob)

    def format_site_info(self, row, t):
        blob = "<highlight>%s %d</highlight> (QL %d-%d) %s\n" % (row.playfield_short_name, row.site_number, row.min_ql, row.max_ql, row.site_name)

        if row.get("org_name"):
            current_day_time = t % 86400
            value = datetime.fromtimestamp(row.close_time, tz=pytz.UTC)
            current_status_time = row.close_time - current_day_time
            if current_status_time < 0:
                current_status_time += 86400

            status = ""
            if current_status_time <= 3600:
                status += "<red>5%%</red> (closes in %s)" % self.util.time_to_readable(current_status_time)
            elif current_status_time <= (3600 * 6):
                status += "<orange>25%%</orange> (closes in %s)" % self.util.time_to_readable(current_status_time)
            else:
                status += "<green>75%%</green> (opens in %s)" % self.util.time_to_readable(current_status_time - (3600 * 6))

            if row.penalty_until > t:
                status += " <red>Penalty</red> (for %s)" % self.util.time_to_readable(row.penalty_until - t)

            blob += "%s (%d) [%s] <highlight>QL %d</highlight> %s %s\n" % (
                row.org_name,
                row.org_id,
                self.text.get_formatted_faction(row.faction),
                row.ql,
                self.text.make_chatcmd("%dx%d" % (row.x_coord, row.y_coord), "/waypoint %d %d %d" % (row.x_coord, row.y_coord, row.playfield_id)),
                self.util.time_to_readable(t - row.created_at))
            blob += "Close Time: <highlight>%s</highlight> %s\n" % (value.strftime("%H:%M:%S %Z"), status)
        else:
            blob += "%s\n" % self.text.make_chatcmd("%dx%d" % (row.x_coord, row.y_coord), "/waypoint %d %d %d" % (row.x_coord, row.y_coord, row.playfield_id))
            if not row.enabled:
                blob += "<red>Disabled</red>\n"

        return blob

    def get_tower_site_info(self, playfield_id, site_number):
        if FeatureFlags.USE_TOWER_API:
            params = list()
            params.append(("playfield_id", playfield_id))
            if site_number:
                params.append(("site_number", site_number))

            data = self.lookup_tower_info(params).results
        else:
            if site_number:
                data = self.db.query("SELECT t.*, p.short_name AS playfield_short_name, p.long_name AS playfield_long_name "
                                     "FROM tower_site t JOIN playfields p ON t.playfield_id = p.id WHERE t.playfield_id = ? AND site_number = ?",
                                     [playfield_id, site_number])
            else:
                data = self.db.query("SELECT t.*, p.short_name AS playfield_short_name, p.long_name AS playfield_long_name "
                                     "FROM tower_site t JOIN playfields p ON t.playfield_id = p.id WHERE t.playfield_id = ?",
                                     [playfield_id])

        return data

    def lookup_tower_info(self, params):
        url = self.setting_service.get("tower_api_address").get_value()

        headers = self.setting_service.get("tower_api_custom_headers").get_value() or {}
        headers.update({"User-Agent": f"Tyrbot {self.bot.version}"})
        r = requests.get(url, params, headers=headers, timeout=5)
        result = DictObject(r.json())

        return result

    def get_lc_blob_footer(self):
        return "Thanks to Draex and Unk for providing the tower information. And a special thanks to Trey."

    def get_ct_type(self, ql):
        if ql < 34:
            return "I"
        elif ql < 82:
            return "II"
        elif ql < 129:
            return "III"
        elif ql < 177:
            return "IV"
        elif ql < 201:
            return "V"
        elif ql < 226:
            return "VI"
        else:
            return "VII"
