#!/usr/bin/env python3
import fontforge

SRC = "/Users/axelmansson/Downloads/nasalization (3)/Nasalization Rg.otf"
OUT20 = "/Users/axelmansson/Documents/GitHub/CPY talking clock/fonts/Nasalization-Regular-20.bdf"
OUT40 = "/Users/axelmansson/Documents/GitHub/CPY talking clock/fonts/Nasalization-Regular-40.bdf"


def generate_bdf(size, out_path, family_name):
    font = fontforge.open(SRC)
    font.encoding = "UnicodeFull"
    font.reencode("unicode")
    font.fullname = family_name
    font.familyname = family_name
    font.fontname = family_name
    font.bitmapSizes = (size,)
    font.generate(out_path)
    print("generated", out_path)


def main():
    generate_bdf(20, OUT20, "Nasalization-Regular-20")
    generate_bdf(40, OUT40, "Nasalization-Regular-40")


if __name__ == "__main__":
    main()
