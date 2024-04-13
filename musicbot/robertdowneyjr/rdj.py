from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from os.path import abspath
from discord import File

def rdj(text: str, size: int = 64, flip: bool = False) -> File:
    """
    Lets you make Robert Downey Jr. do your bidding.

    Overlays provided text on top of static image of RDJ.

    Parameters:
    text (str): the text to overlay, as a multiline string
    size (int): the text size
    flip (bool): whether or not to flip RDJ (defaults to false)
    """
    with Image.open(abspath("musicbot/robertdowneyjr/rdj_template_white.png")) as robert:
        middle = (400, 360)
        font = ImageFont.truetype(abspath("musicbot/robertdowneyjr/OpenSans-Regular.ttf"), size)
        if (flip):
            robert = robert.transpose(method=Image.Transpose.FLIP_LEFT_RIGHT)
            middle = (880, 360)
        draw = ImageDraw.Draw(robert)
        
        draw.text(middle, text, fill="black", font=font, anchor="mm", align="center")
        #robert.show()
        with BytesIO() as output_binary: #No idea what this does
            robert.save(output_binary, format="JPEG")
            output_binary.seek(0)
            return File(fp=output_binary, filename='rdj.jpeg') #Need to return as discord.File because output_binary closes after this function returns
    
if __name__=="__main__":
    rdj("Test Text", size=100, flip=True) #this is non-functional
