from finquint.pipeline import PipelineContext, PipelineStage, QuantPipeline

class AddStage(PipelineStage):
    def __init__(self, x): self.x=x
    def run(self, data, context):
        context.set("last_amount", self.x)
        return data+self.x

def test_pipeline():
    assert QuantPipeline().add(AddStage(2)).add(AddStage(3)).run(10) == 15
