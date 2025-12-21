from lxml import etree
try:
    etree.parse("light_mode.svg")
    print("OK: light_mode.svg parses")
except etree.XMLSyntaxError as e:
    print("PARSE ERROR:", e)
