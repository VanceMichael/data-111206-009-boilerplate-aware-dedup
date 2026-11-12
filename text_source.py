from html.parser import HTMLParser


class Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.counts = [{}]
        self.results = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in ('meta','link','br','hr','img','input','source','wbr'):
            return
        attributes = dict(attrs)
        count = self.counts[-1].get(tag,0)+1
        self.counts[-1][tag]=count
        self.stack.append(f'{tag}[{count}]')
        self.counts.append({})
        style=(attributes.get('style') or '').replace(' ','').lower()
        self.hidden.append(tag in ('script','style','head') or 'hidden' in attributes or 'display:none' in style or attributes.get('aria-hidden')=='true')

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index].split('[')[0]==tag:
                self.stack=self.stack[:index]
                self.hidden=self.hidden[:index]
                self.counts=self.counts[:index+1]
                break

    def handle_data(self, data):
        text=' '.join(data.split())
        if text and not any(self.hidden):
            self.results.append({'path':'/'+ '/'.join(self.stack), 'text':text, 'ordinal':len(self.results)})


def extract(html):
    parser=Blocks()
    parser.feed(html)
    return parser.results
