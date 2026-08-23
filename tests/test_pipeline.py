import pytest

from finquint.pipeline import PipelineContext, PipelineStage, QuantPipeline


class AddOne(PipelineStage):
    name = "add_one"

    def run(self, data, context):
        context.set("seen", True)
        return data + 1


class Fail(PipelineStage):
    name = "fail"

    def run(self, data, context):
        raise ValueError("expected failure")


def test_pipeline_tracks_results_and_metrics():
    result = QuantPipeline().add(AddOne()).run(1)
    assert result.data == 2
    assert result.context.get("seen") is True
    assert result.context.metrics["add_one"]["status"] == "success"


def test_pipeline_records_and_raises_errors():
    context = PipelineContext()
    with pytest.raises(ValueError, match="expected failure"):
        QuantPipeline().add(Fail()).run(context=context)
    assert context.errors == [{"stage": "fail", "error": "expected failure"}]


def test_continue_on_error():
    result = QuantPipeline(continue_on_error=True).add(Fail()).add(AddOne()).run(1)
    assert result.data == 2
    assert result.context.metrics["fail"]["status"] == "failed"

