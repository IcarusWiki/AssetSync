"""Keep icarus.wiki.gg game images in sync with the game's Assets/2DArt/UI textures."""

# Texture paths are stored relative to the game's content mount, e.g.
# "Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns/ITEM_Pistol_Handgun".
SCOPE_PREFIX = "Assets/2DArt/UI/"
# Ue4Export and the /Game/ references in data tables address assets under this prefix.
CONTENT_PREFIX = "Icarus/Content/"
