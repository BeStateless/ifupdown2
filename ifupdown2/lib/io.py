# Copyright (C) 2017, 2018 Cumulus Networks, Inc. all rights reserved
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; version 2.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.
#
# https://www.gnu.org/licenses/gpl-2.0-standalone.html
#
# Author:
#       Julien Fortin, julien@cumulusnetworks.com
#
# io -- all io (file) handlers
#

try:
    from ifupdown2.lib.base_objects import BaseObject
except ImportError:
    from lib.base_objects import BaseObject


class IO(BaseObject):
    def __init__(self):
        BaseObject.__init__(self)

    def write_to_file(self, path, string):
        try:
            self.logger.info("writing '%s' to file %s" % (string, path))
            with open(path, "w") as f:
                f.write(string)
        except IOError, e:
            self.logger.warn("error writing to file %s: %s" % (path, str(e)))
            return -1
        return 0

    def write_to_file_dry_run(self, path, string):
        self.logger.info("writing '%s' to file %s" % (string, path))
        return 0
