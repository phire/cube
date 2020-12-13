from nmigen import Signal

def acclumnateOR(comb, items):
    result = items[0]
    for item in items[1:]:
        new_result = Signal(item.width)
        comb += new_result.eq(result | item)
        result = new_result
    return result