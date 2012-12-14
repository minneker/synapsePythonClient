## Check for latest version and recommend upgrade
##
############################################################
from distutils.version import StrictVersion
from utils import downloadFile
import requests
import json
import sys
import traceback
import warnings


CURRENT_VERSION = "0.1.1"


## Get the latest version information from version_url and check against
## the current version. Recommend upgrade, if a newer version exists.
def version_check(current_version=CURRENT_VERSION,
				  version_url="http://dev-versions.synapse.sagebase.org/synapsePythonClient",
				  upgrade_url="https://github.com/Sage-Bionetworks/synapsePythonClient"):

	try:
		headers = { 'Accept': 'application/json' }
		version_info = requests.get(version_url, headers=headers).json

		## check blacklist
		if current_version in version_info['blacklist']:
			msg = "\nPLEASE UPGRADE YOUR CLIENT\n\nUpgrading your SynapseClient is required. Please visit:\n%s\n\n" % (upgrade_url,)
			raise SystemExit(msg)

		## check latest version
		if StrictVersion(current_version) < StrictVersion(version_info['latestVersion']):
			msg = "\nUPGRADE AVAILABLE\n\nA more recent version of the Synapse Client (%s) is available. Your version (%s) can be upgraded by visiting:\n%s\n\n" % (version_info['latestVersion'], current_version, upgrade_url,)
			sys.stderr.write(msg)
			return False

	except Exception, e:
		## don't prevent the client from running if something goes wrong
		sys.stderr.write("Exception in version check.\n")
		return False

	return True

if __name__ == "__main__":
    version_check()
