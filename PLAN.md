Script will:

If the current script will require installing any modules, have the current script, upon startup and before taking any other action:
- Determine if it is in a venv
    - if so, activate it
    - if not, create one, then activate it
- Once the venv is activated, ensure that all requirements from requirements.txt are installed in the venv
- Proceed, preserving any command line switches that were originally given.

Possible command line switches include
--moon
--foliate
--date
--position
--help

It will read directories from .env in the directory it is run from, one directory per line.  Each line is a couplet of  APPLICATION:directory
In this case, the directories point to examples that have functional data in them.

Using the insights from files in ./1_reference, write a script that will *bidirectionally* sync reading position for Moon+ and Foliate.

The script will first identify matches.  For Moon+, it gets the
`Title - Author`
from the filename.  
For Foliate, it checks the json files in the root data directory (such as `9780765389206.json`) and parses the json to find the Title and author:name fields to match.

Then it compares position (see Moon_Format.md and Foliate.md).  By default, it will sync both reading states.  How it resolves differences is based on the command line switches.
--position
    the default, greatest position read/furthest in book wins
--date
    Conflict resolved by which source has a later "modified" date
--moon  
    Moon+ position wins
--foliate
    Foliate position wins.

For now the script will skip any entries that do not exist on both sides.
